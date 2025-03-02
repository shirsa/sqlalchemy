import dataclasses

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import testing
from sqlalchemy import update
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Composite
from sqlalchemy.orm import composite
from sqlalchemy.orm import configure_mappers
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session
from sqlalchemy.testing import assert_raises_message
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing.fixtures import fixture_session
from sqlalchemy.testing.schema import Column
from sqlalchemy.testing.schema import Table


class PointTest(fixtures.MappedTest, testing.AssertsCompiledSQL):
    __dialect__ = "default"

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "graphs",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("name", String(30)),
        )

        Table(
            "edges",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("graph_id", Integer, ForeignKey("graphs.id")),
            Column("x1", Integer),
            Column("y1", Integer),
            Column("x2", Integer),
            Column("y2", Integer),
        )

    @classmethod
    def setup_mappers(cls):
        graphs, edges = cls.tables.graphs, cls.tables.edges

        class Point(cls.Comparable):
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def __composite_values__(self):
                return [self.x, self.y]

            __hash__ = None

            def __eq__(self, other):
                return (
                    isinstance(other, Point)
                    and other.x == self.x
                    and other.y == self.y
                )

            def __ne__(self, other):
                return not isinstance(other, Point) or not self.__eq__(other)

        class Graph(cls.Comparable):
            pass

        class Edge(cls.Comparable):
            def __init__(self, *args):
                if args:
                    self.start, self.end = args

        cls.mapper_registry.map_imperatively(
            Graph, graphs, properties={"edges": relationship(Edge)}
        )
        cls.mapper_registry.map_imperatively(
            Edge,
            edges,
            properties={
                "start": sa.orm.composite(Point, edges.c.x1, edges.c.y1),
                "end": sa.orm.composite(Point, edges.c.x2, edges.c.y2),
            },
        )

    def _fixture(self):
        Graph, Edge, Point = (
            self.classes.Graph,
            self.classes.Edge,
            self.classes.Point,
        )

        sess = Session(testing.db)
        g = Graph(
            id=1,
            edges=[
                Edge(Point(3, 4), Point(5, 6)),
                Edge(Point(14, 5), Point(2, 7)),
            ],
        )
        sess.add(g)
        sess.commit()
        return sess

    def test_early_configure(self):
        # test [ticket:2935], that we can call a composite
        # expression before configure_mappers()
        Edge = self.classes.Edge
        Edge.start.__clause_element__()

    def test_round_trip(self):
        Graph, Point = self.classes.Graph, self.classes.Point

        sess = self._fixture()

        g1 = sess.query(Graph).first()
        sess.close()

        g = sess.get(Graph, g1.id)
        eq_(
            [(e.start, e.end) for e in g.edges],
            [(Point(3, 4), Point(5, 6)), (Point(14, 5), Point(2, 7))],
        )

    def test_detect_change(self):
        Graph, Edge, Point = (
            self.classes.Graph,
            self.classes.Edge,
            self.classes.Point,
        )

        sess = self._fixture()

        g = sess.query(Graph).first()
        g.edges[1].end = Point(18, 4)
        sess.commit()

        e = sess.get(Edge, g.edges[1].id)
        eq_(e.end, Point(18, 4))

    def test_not_none(self):
        Edge = self.classes.Edge

        # current contract.   the composite is None
        # when hasn't been populated etc. on a
        # pending/transient object.
        e1 = Edge()
        assert e1.end is None
        sess = fixture_session()
        sess.add(e1)

        # however, once it's persistent, the code as of 0.7.3
        # would unconditionally populate it, even though it's
        # all None.  I think this usage contract is inconsistent,
        # and it would be better that the composite is just
        # created unconditionally in all cases.
        # but as we are just trying to fix [ticket:2308] and
        # [ticket:2309] without changing behavior we maintain
        # that only "persistent" gets the composite with the
        # Nones

        sess.flush()
        assert e1.end is not None

    def test_eager_load(self):
        Graph, Point = self.classes.Graph, self.classes.Point

        sess = self._fixture()

        g = sess.query(Graph).first()
        sess.close()

        def go():
            g2 = sess.get(
                Graph, g.id, options=[sa.orm.joinedload(Graph.edges)]
            )

            eq_(
                [(e.start, e.end) for e in g2.edges],
                [(Point(3, 4), Point(5, 6)), (Point(14, 5), Point(2, 7))],
            )

        self.assert_sql_count(testing.db, go, 1)

    def test_comparator(self):
        Graph, Edge, Point = (
            self.classes.Graph,
            self.classes.Edge,
            self.classes.Point,
        )

        sess = self._fixture()

        g = sess.query(Graph).first()

        assert (
            sess.query(Edge).filter(Edge.start == Point(3, 4)).one()
            is g.edges[0]
        )

        assert (
            sess.query(Edge).filter(Edge.start != Point(3, 4)).first()
            is g.edges[1]
        )

        eq_(sess.query(Edge).filter(Edge.start == None).all(), [])  # noqa

    def test_comparator_aliased(self):
        Graph, Edge, Point = (
            self.classes.Graph,
            self.classes.Edge,
            self.classes.Point,
        )

        sess = self._fixture()

        g = sess.query(Graph).first()
        ea = aliased(Edge)
        assert (
            sess.query(ea).filter(ea.start != Point(3, 4)).first()
            is g.edges[1]
        )

    def test_bulk_update_sql(self):
        Edge, Point = (self.classes.Edge, self.classes.Point)

        sess = self._fixture()

        e1 = sess.execute(
            select(Edge).filter(Edge.start == Point(14, 5))
        ).scalar_one()

        eq_(e1.end, Point(2, 7))

        stmt = (
            update(Edge)
            .filter(Edge.start == Point(14, 5))
            .values({Edge.end: Point(16, 10)})
        )

        self.assert_compile(
            stmt,
            "UPDATE edges SET x2=:x2, y2=:y2 WHERE edges.x1 = :x1_1 "
            "AND edges.y1 = :y1_1",
            params={"x2": 16, "x1_1": 14, "y2": 10, "y1_1": 5},
            dialect="default",
        )

    def test_bulk_update_evaluate(self):
        Edge, Point = (self.classes.Edge, self.classes.Point)

        sess = self._fixture()

        e1 = sess.execute(
            select(Edge).filter(Edge.start == Point(14, 5))
        ).scalar_one()

        eq_(e1.end, Point(2, 7))

        stmt = (
            update(Edge)
            .filter(Edge.start == Point(14, 5))
            .values({Edge.end: Point(16, 10)})
        )
        sess.execute(stmt)

        eq_(e1.end, Point(16, 10))

        stmt = (
            update(Edge)
            .filter(Edge.start == Point(14, 5))
            .values({Edge.end: Point(17, 8)})
        )
        sess.execute(stmt)

        eq_(e1.end, Point(17, 8))

    def test_bulk_update_fetch(self):
        Edge, Point = (self.classes.Edge, self.classes.Point)

        sess = self._fixture()

        e1 = sess.query(Edge).filter(Edge.start == Point(14, 5)).one()

        eq_(e1.end, Point(2, 7))

        q = sess.query(Edge).filter(Edge.start == Point(14, 5))
        q.update({Edge.end: Point(16, 10)}, synchronize_session="fetch")

        eq_(e1.end, Point(16, 10))

        q.update({Edge.end: Point(17, 8)}, synchronize_session="fetch")

        eq_(e1.end, Point(17, 8))

    def test_get_history(self):
        Edge = self.classes.Edge
        Point = self.classes.Point
        from sqlalchemy.orm.attributes import get_history

        e1 = Edge()
        e1.start = Point(1, 2)
        eq_(
            get_history(e1, "start"),
            ([Point(x=1, y=2)], (), [Point(x=None, y=None)]),
        )

        eq_(get_history(e1, "end"), ((), [Point(x=None, y=None)], ()))

    def test_query_cols_legacy(self):
        Edge = self.classes.Edge

        sess = self._fixture()

        eq_(
            sess.query(Edge.start.clauses, Edge.end.clauses).all(),
            [(3, 4, 5, 6), (14, 5, 2, 7)],
        )

    def test_query_cols(self):
        Edge = self.classes.Edge
        Point = self.classes.Point

        sess = self._fixture()

        start, end = Edge.start, Edge.end

        eq_(
            sess.query(start, end).filter(start == Point(3, 4)).all(),
            [(Point(3, 4), Point(5, 6))],
        )

    def test_cols_as_core_clauseelement(self):
        Edge = self.classes.Edge
        Point = self.classes.Point

        start, end = Edge.start, Edge.end

        stmt = select(start, end).where(start == Point(3, 4))
        self.assert_compile(
            stmt,
            "SELECT edges.x1, edges.y1, edges.x2, edges.y2 "
            "FROM edges WHERE edges.x1 = :x1_1 AND edges.y1 = :y1_1",
            checkparams={"x1_1": 3, "y1_1": 4},
        )

    def test_query_cols_labeled(self):
        Edge = self.classes.Edge
        Point = self.classes.Point

        sess = self._fixture()

        start, end = Edge.start, Edge.end

        row = (
            sess.query(start.label("s1"), end)
            .filter(start == Point(3, 4))
            .first()
        )
        eq_(row.s1.x, 3)
        eq_(row.s1.y, 4)
        eq_(row.end.x, 5)
        eq_(row.end.y, 6)

    def test_delete(self):
        Point = self.classes.Point
        Graph, Edge = self.classes.Graph, self.classes.Edge

        sess = self._fixture()
        g = sess.query(Graph).first()

        e = g.edges[1]
        del e.end
        sess.flush()
        eq_(
            sess.query(Edge.start, Edge.end).all(),
            [
                (Point(x=3, y=4), Point(x=5, y=6)),
                (Point(x=14, y=5), Point(x=None, y=None)),
            ],
        )

    def test_save_null(self):
        """test saving a null composite value

        See google groups thread for more context:
        https://groups.google.com/group/sqlalchemy/browse_thread/thread/0c6580a1761b2c29

        """

        Graph, Edge = self.classes.Graph, self.classes.Edge

        sess = fixture_session()
        g = Graph(id=1)
        e = Edge(None, None)
        g.edges.append(e)

        sess.add(g)
        sess.commit()

        g2 = sess.get(Graph, 1)
        assert g2.edges[-1].start.x is None
        assert g2.edges[-1].start.y is None

    def test_expire(self):
        Graph, Point = self.classes.Graph, self.classes.Point

        sess = self._fixture()
        g = sess.query(Graph).first()
        e = g.edges[0]
        sess.expire(e)
        assert "start" not in e.__dict__
        assert e.start == Point(3, 4)

    def test_default_value(self):
        Edge = self.classes.Edge

        e = Edge()
        eq_(e.start, None)

    def test_no_name_declarative(self, decl_base, connection):
        """test #7751"""

        class Point:
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def __composite_values__(self):
                return self.x, self.y

            def __repr__(self):
                return "Point(x=%r, y=%r)" % (self.x, self.y)

            def __eq__(self, other):
                return (
                    isinstance(other, Point)
                    and other.x == self.x
                    and other.y == self.y
                )

            def __ne__(self, other):
                return not self.__eq__(other)

        class Vertex(decl_base):
            __tablename__ = "vertices"

            id = Column(Integer, primary_key=True)
            x1 = Column(Integer)
            y1 = Column(Integer)
            x2 = Column(Integer)
            y2 = Column(Integer)

            start = composite(Point, x1, y1)
            end = composite(Point, x2, y2)

        self.assert_compile(
            select(Vertex),
            "SELECT vertices.id, vertices.x1, vertices.y1, vertices.x2, "
            "vertices.y2 FROM vertices",
        )

        decl_base.metadata.create_all(connection)
        s = Session(connection)
        hv = Vertex(start=Point(1, 2), end=Point(3, 4))
        s.add(hv)
        s.commit()

        is_(
            hv,
            s.scalars(
                select(Vertex).where(Vertex.start == Point(1, 2))
            ).first(),
        )

    def test_no_name_declarative_two(self, decl_base, connection):
        """test #7752"""

        class Point:
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def __composite_values__(self):
                return self.x, self.y

            def __repr__(self):
                return "Point(x=%r, y=%r)" % (self.x, self.y)

            def __eq__(self, other):
                return (
                    isinstance(other, Point)
                    and other.x == self.x
                    and other.y == self.y
                )

            def __ne__(self, other):
                return not self.__eq__(other)

        class Vertex:
            def __init__(self, start, end):
                self.start = start
                self.end = end

            @classmethod
            def _generate(self, x1, y1, x2, y2):
                """generate a Vertex from a row"""
                return Vertex(Point(x1, y1), Point(x2, y2))

            def __composite_values__(self):
                return (
                    self.start.__composite_values__()
                    + self.end.__composite_values__()
                )

        class HasVertex(decl_base):
            __tablename__ = "has_vertex"
            id = Column(Integer, primary_key=True)
            x1 = Column(Integer)
            y1 = Column(Integer)
            x2 = Column(Integer)
            y2 = Column(Integer)

            vertex = composite(Vertex._generate, x1, y1, x2, y2)

        self.assert_compile(
            select(HasVertex),
            "SELECT has_vertex.id, has_vertex.x1, has_vertex.y1, "
            "has_vertex.x2, has_vertex.y2 FROM has_vertex",
        )

        decl_base.metadata.create_all(connection)
        s = Session(connection)
        hv = HasVertex(vertex=Vertex(Point(1, 2), Point(3, 4)))
        s.add(hv)
        s.commit()
        is_(
            hv,
            s.scalars(
                select(HasVertex).where(
                    HasVertex.vertex == Vertex(Point(1, 2), Point(3, 4))
                )
            ).first(),
        )


class NestedTest(fixtures.MappedTest, testing.AssertsCompiledSQL):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "stuff",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("a", String(30)),
            Column("b", String(30)),
            Column("c", String(30)),
            Column("d", String(30)),
        )

    def _fixture(self):
        class AB:
            def __init__(self, a, b, cd):
                self.a = a
                self.b = b
                self.cd = cd

            @classmethod
            def generate(cls, a, b, c, d):
                return AB(a, b, CD(c, d))

            def __composite_values__(self):
                return (self.a, self.b) + self.cd.__composite_values__()

            def __eq__(self, other):
                return (
                    isinstance(other, AB)
                    and self.a == other.a
                    and self.b == other.b
                    and self.cd == other.cd
                )

            def __ne__(self, other):
                return not self.__eq__(other)

        class CD:
            def __init__(self, c, d):
                self.c = c
                self.d = d

            def __composite_values__(self):
                return (self.c, self.d)

            def __eq__(self, other):
                return (
                    isinstance(other, CD)
                    and self.c == other.c
                    and self.d == other.d
                )

            def __ne__(self, other):
                return not self.__eq__(other)

        class Thing:
            def __init__(self, ab):
                self.ab = ab

        stuff = self.tables.stuff
        self.mapper_registry.map_imperatively(
            Thing,
            stuff,
            properties={
                "ab": composite(
                    AB.generate, stuff.c.a, stuff.c.b, stuff.c.c, stuff.c.d
                )
            },
        )
        return Thing, AB, CD

    def test_round_trip(self):
        Thing, AB, CD = self._fixture()

        s = fixture_session()

        s.add(Thing(AB("a", "b", CD("c", "d"))))
        s.commit()

        s.close()

        t1 = (
            s.query(Thing).filter(Thing.ab == AB("a", "b", CD("c", "d"))).one()
        )
        eq_(t1.ab, AB("a", "b", CD("c", "d")))


class PrimaryKeyTest(fixtures.MappedTest):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "graphs",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("version_id", Integer, primary_key=True, nullable=True),
            Column("name", String(30)),
        )

    @classmethod
    def setup_mappers(cls):
        graphs = cls.tables.graphs

        class Version(cls.Comparable):
            def __init__(self, id_, version):
                self.id = id_
                self.version = version

            def __composite_values__(self):
                return (self.id, self.version)

            __hash__ = None

            def __eq__(self, other):
                return (
                    isinstance(other, Version)
                    and other.id == self.id
                    and other.version == self.version
                )

            def __ne__(self, other):
                return not self.__eq__(other)

        class Graph(cls.Comparable):
            def __init__(self, version):
                self.version = version

        cls.mapper_registry.map_imperatively(
            Graph,
            graphs,
            properties={
                "version": sa.orm.composite(
                    Version, graphs.c.id, graphs.c.version_id
                )
            },
        )

    def _fixture(self):
        Graph, Version = self.classes.Graph, self.classes.Version

        sess = fixture_session()
        g = Graph(Version(1, 1))
        sess.add(g)
        sess.commit()
        return sess

    def test_get_by_col(self):
        Graph = self.classes.Graph

        sess = self._fixture()
        g = sess.query(Graph).first()

        g2 = sess.get(Graph, [g.id, g.version_id])
        eq_(g.version, g2.version)

    def test_get_by_composite(self):
        Graph, Version = self.classes.Graph, self.classes.Version

        sess = self._fixture()
        g = sess.query(Graph).first()

        g2 = sess.get(Graph, Version(g.id, g.version_id))
        eq_(g.version, g2.version)

    def test_pk_mutation(self):
        Graph, Version = self.classes.Graph, self.classes.Version

        sess = self._fixture()

        g = sess.query(Graph).first()

        g.version = Version(2, 1)
        sess.commit()
        g2 = sess.get(Graph, Version(2, 1))
        eq_(g.version, g2.version)

    @testing.fails_on_everything_except("sqlite")
    def test_null_pk(self):
        Graph, Version = self.classes.Graph, self.classes.Version

        sess = fixture_session()

        # test pk with one column NULL
        # only sqlite can really handle this
        g = Graph(Version(2, None))
        sess.add(g)
        sess.commit()
        g2 = sess.query(Graph).filter_by(version=Version(2, None)).one()
        eq_(g.version, g2.version)


class PrimaryKeyTestDataclasses(PrimaryKeyTest):
    @classmethod
    def setup_mappers(cls):
        graphs = cls.tables.graphs

        @dataclasses.dataclass
        class Version:
            id: int
            version: int

        cls.classes.Version = Version

        class Graph(cls.Comparable):
            def __init__(self, version):
                self.version = version

        cls.mapper_registry.map_imperatively(
            Graph,
            graphs,
            properties={
                "version": sa.orm.composite(
                    Version, graphs.c.id, graphs.c.version_id
                )
            },
        )


class DefaultsTest(fixtures.MappedTest):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "foobars",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("x1", Integer, default=2),
            Column("x2", Integer),
            Column("x3", Integer, server_default="15"),
            Column("x4", Integer),
        )

    @classmethod
    def setup_mappers(cls):
        foobars = cls.tables.foobars

        class Foobar(cls.Comparable):
            pass

        class FBComposite(cls.Comparable):
            def __init__(self, x1, x2, x3, x4):
                self.goofy_x1 = x1
                self.x2 = x2
                self.x3 = x3
                self.x4 = x4

            def __composite_values__(self):
                return self.goofy_x1, self.x2, self.x3, self.x4

            __hash__ = None

            def __eq__(self, other):
                return (
                    other.goofy_x1 == self.goofy_x1
                    and other.x2 == self.x2
                    and other.x3 == self.x3
                    and other.x4 == self.x4
                )

            def __ne__(self, other):
                return not self.__eq__(other)

            def __repr__(self):
                return "FBComposite(%r, %r, %r, %r)" % (
                    self.goofy_x1,
                    self.x2,
                    self.x3,
                    self.x4,
                )

        cls.mapper_registry.map_imperatively(
            Foobar,
            foobars,
            properties=dict(
                foob=sa.orm.composite(
                    FBComposite,
                    foobars.c.x1,
                    foobars.c.x2,
                    foobars.c.x3,
                    foobars.c.x4,
                )
            ),
        )

    def test_attributes_with_defaults(self):
        Foobar, FBComposite = self.classes.Foobar, self.classes.FBComposite

        sess = fixture_session()
        f1 = Foobar()
        f1.foob = FBComposite(None, 5, None, None)
        sess.add(f1)
        sess.flush()

        eq_(f1.foob, FBComposite(2, 5, 15, None))

        f2 = Foobar()
        sess.add(f2)
        sess.flush()
        eq_(f2.foob, FBComposite(2, None, 15, None))

    def test_set_composite_values(self):
        Foobar, FBComposite = self.classes.Foobar, self.classes.FBComposite

        sess = fixture_session()
        f1 = Foobar()
        f1.foob = FBComposite(None, 5, None, None)
        sess.add(f1)
        sess.flush()

        eq_(f1.foob, FBComposite(2, 5, 15, None))


class MappedSelectTest(fixtures.MappedTest):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "descriptions",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("d1", String(20)),
            Column("d2", String(20)),
        )

        Table(
            "values",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column(
                "description_id",
                Integer,
                ForeignKey("descriptions.id"),
                nullable=False,
            ),
            Column("v1", String(20)),
            Column("v2", String(20)),
        )

    @classmethod
    def setup_mappers(cls):
        values, descriptions = cls.tables.values, cls.tables.descriptions

        class Descriptions(cls.Comparable):
            pass

        class Values(cls.Comparable):
            pass

        class CustomValues(cls.Comparable, list):
            def __init__(self, *args):
                self.extend(args)

            def __composite_values__(self):
                return self

        desc_values = (
            select(values, descriptions.c.d1, descriptions.c.d2)
            .where(
                descriptions.c.id == values.c.description_id,
            )
            .alias("descriptions_values")
        )

        cls.mapper_registry.map_imperatively(
            Descriptions,
            descriptions,
            properties={
                "values": relationship(Values, lazy="dynamic"),
                "custom_descriptions": composite(
                    CustomValues, descriptions.c.d1, descriptions.c.d2
                ),
            },
        )

        cls.mapper_registry.map_imperatively(
            Values,
            desc_values,
            properties={
                "custom_values": composite(
                    CustomValues, desc_values.c.v1, desc_values.c.v2
                )
            },
        )

    def test_set_composite_attrs_via_selectable(self, connection):
        Values, CustomValues, values, Descriptions, descriptions = (
            self.classes.Values,
            self.classes.CustomValues,
            self.tables.values,
            self.classes.Descriptions,
            self.tables.descriptions,
        )

        session = fixture_session()
        d = Descriptions(
            custom_descriptions=CustomValues("Color", "Number"),
            values=[
                Values(custom_values=CustomValues("Red", "5")),
                Values(custom_values=CustomValues("Blue", "1")),
            ],
        )

        session.add(d)
        session.commit()
        eq_(
            connection.execute(descriptions.select()).fetchall(),
            [(1, "Color", "Number")],
        )
        eq_(
            connection.execute(values.select()).fetchall(),
            [(1, 1, "Red", "5"), (2, 1, "Blue", "1")],
        )


class ManyToOneTest(fixtures.MappedTest):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "a",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("b1", String(20)),
            Column("b2_id", Integer, ForeignKey("b.id")),
        )

        Table(
            "b",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("data", String(20)),
        )

    @classmethod
    def setup_mappers(cls):
        a, b = cls.tables.a, cls.tables.b

        class A(cls.Comparable):
            pass

        class B(cls.Comparable):
            pass

        class C(cls.Comparable):
            def __init__(self, b1, b2):
                self.b1, self.b2 = b1, b2

            def __composite_values__(self):
                return self.b1, self.b2

            def __eq__(self, other):
                return (
                    isinstance(other, C)
                    and other.b1 == self.b1
                    and other.b2 == self.b2
                )

        cls.mapper_registry.map_imperatively(
            A,
            a,
            properties={"b2": relationship(B), "c": composite(C, "b1", "b2")},
        )
        cls.mapper_registry.map_imperatively(B, b)

    def test_early_configure(self):
        # test [ticket:2935], that we can call a composite
        # expression before configure_mappers()
        A = self.classes.A
        A.c.__clause_element__()

    def test_persist(self):
        A, C, B = (self.classes.A, self.classes.C, self.classes.B)

        sess = fixture_session()
        sess.add(A(c=C("b1", B(data="b2"))))
        sess.commit()

        a1 = sess.query(A).one()
        eq_(a1.c, C("b1", B(data="b2")))

    def test_query(self):
        A, C, B = (self.classes.A, self.classes.C, self.classes.B)

        sess = fixture_session()
        b1, b2 = B(data="b1"), B(data="b2")
        a1 = A(c=C("a1b1", b1))
        a2 = A(c=C("a2b1", b2))
        sess.add_all([a1, a2])
        sess.commit()

        eq_(sess.query(A).filter(A.c == C("a2b1", b2)).one(), a2)

    def test_query_aliased(self):
        A, C, B = (self.classes.A, self.classes.C, self.classes.B)

        sess = fixture_session()
        b1, b2 = B(data="b1"), B(data="b2")
        a1 = A(c=C("a1b1", b1))
        a2 = A(c=C("a2b1", b2))
        sess.add_all([a1, a2])
        sess.commit()

        ae = aliased(A)
        eq_(sess.query(ae).filter(ae.c == C("a2b1", b2)).one(), a2)


class ConfigurationTest(fixtures.MappedTest):
    @classmethod
    def define_tables(cls, metadata):
        Table(
            "edge",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("x1", Integer),
            Column("y1", Integer),
            Column("x2", Integer),
            Column("y2", Integer),
        )

    @classmethod
    def setup_mappers(cls):
        class Point(cls.Comparable):
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def __composite_values__(self):
                return [self.x, self.y]

            def __eq__(self, other):
                return (
                    isinstance(other, Point)
                    and other.x == self.x
                    and other.y == self.y
                )

            def __ne__(self, other):
                return not isinstance(other, Point) or not self.__eq__(other)

        class Edge(cls.Comparable):
            pass

    def _test_roundtrip(self):
        Edge, Point = self.classes.Edge, self.classes.Point

        e1 = Edge(start=Point(3, 4), end=Point(5, 6))
        sess = fixture_session()
        sess.add(e1)
        sess.commit()

        eq_(sess.query(Edge).one(), Edge(start=Point(3, 4), end=Point(5, 6)))

    def test_columns(self):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )

        self.mapper_registry.map_imperatively(
            Edge,
            edge,
            properties={
                "start": sa.orm.composite(Point, edge.c.x1, edge.c.y1),
                "end": sa.orm.composite(Point, edge.c.x2, edge.c.y2),
            },
        )

        self._test_roundtrip()

    def test_attributes(self):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )

        m = self.mapper_registry.map_imperatively(Edge, edge)
        m.add_property("start", sa.orm.composite(Point, Edge.x1, Edge.y1))
        m.add_property("end", sa.orm.composite(Point, Edge.x2, Edge.y2))

        self._test_roundtrip()

    def test_strings(self):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )

        m = self.mapper_registry.map_imperatively(Edge, edge)
        m.add_property("start", sa.orm.composite(Point, "x1", "y1"))
        m.add_property("end", sa.orm.composite(Point, "x2", "y2"))

        self._test_roundtrip()

    def test_deferred(self):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )
        self.mapper_registry.map_imperatively(
            Edge,
            edge,
            properties={
                "start": sa.orm.composite(
                    Point, edge.c.x1, edge.c.y1, deferred=True, group="s"
                ),
                "end": sa.orm.composite(
                    Point, edge.c.x2, edge.c.y2, deferred=True
                ),
            },
        )
        self._test_roundtrip()

    def test_check_prop_type(self):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )
        self.mapper_registry.map_imperatively(
            Edge,
            edge,
            properties={
                "start": sa.orm.composite(Point, (edge.c.x1,), edge.c.y1)
            },
        )
        assert_raises_message(
            sa.exc.ArgumentError,
            # note that we also are checking that the tuple
            # renders here, so the "%" operator in the string needs to
            # apply the tuple also
            r"Composite expects Column objects or mapped "
            r"attributes/attribute names as "
            r"arguments, got: \(Column",
            configure_mappers,
        )


class ComparatorTest(fixtures.MappedTest, testing.AssertsCompiledSQL):
    __dialect__ = "default"

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "edge",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("x1", Integer),
            Column("y1", Integer),
            Column("x2", Integer),
            Column("y2", Integer),
        )

    @classmethod
    def setup_mappers(cls):
        class Point(cls.Comparable):
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def __composite_values__(self):
                return [self.x, self.y]

            def __eq__(self, other):
                return (
                    isinstance(other, Point)
                    and other.x == self.x
                    and other.y == self.y
                )

            def __ne__(self, other):
                return not isinstance(other, Point) or not self.__eq__(other)

        class Edge(cls.Comparable):
            def __init__(self, start, end):
                self.start = start
                self.end = end

            def __eq__(self, other):
                return isinstance(other, Edge) and other.id == self.id

    def _fixture(self, custom):
        edge, Edge, Point = (
            self.tables.edge,
            self.classes.Edge,
            self.classes.Point,
        )

        if custom:

            class CustomComparator(sa.orm.Composite.Comparator):
                def near(self, other, d):
                    clauses = self.__clause_element__().clauses
                    diff_x = clauses[0] - other.x
                    diff_y = clauses[1] - other.y
                    return diff_x * diff_x + diff_y * diff_y <= d * d

            self.mapper_registry.map_imperatively(
                Edge,
                edge,
                properties={
                    "start": sa.orm.composite(
                        Point,
                        edge.c.x1,
                        edge.c.y1,
                        comparator_factory=CustomComparator,
                    ),
                    "end": sa.orm.composite(Point, edge.c.x2, edge.c.y2),
                },
            )
        else:
            self.mapper_registry.map_imperatively(
                Edge,
                edge,
                properties={
                    "start": sa.orm.composite(Point, edge.c.x1, edge.c.y1),
                    "end": sa.orm.composite(Point, edge.c.x2, edge.c.y2),
                },
            )

    def test_comparator_behavior_default(self):
        self._fixture(False)
        self._test_comparator_behavior()

    def test_comparator_behavior_custom(self):
        self._fixture(True)
        self._test_comparator_behavior()

    def _test_comparator_behavior(self):
        Edge, Point = (self.classes.Edge, self.classes.Point)

        sess = fixture_session()
        e1 = Edge(Point(3, 4), Point(5, 6))
        e2 = Edge(Point(14, 5), Point(2, 7))
        sess.add_all([e1, e2])
        sess.commit()

        assert sess.query(Edge).filter(Edge.start == Point(3, 4)).one() is e1

        assert sess.query(Edge).filter(Edge.start != Point(3, 4)).first() is e2

        eq_(sess.query(Edge).filter(Edge.start == None).all(), [])  # noqa

    def test_default_comparator_factory(self):
        self._fixture(False)
        Edge = self.classes.Edge
        start_prop = Edge.start.property

        assert start_prop.comparator_factory is Composite.Comparator

    def test_custom_comparator_factory(self):
        self._fixture(True)
        Edge, Point = (self.classes.Edge, self.classes.Point)

        edge_1, edge_2 = (
            Edge(Point(0, 0), Point(3, 5)),
            Edge(Point(0, 1), Point(3, 5)),
        )

        sess = fixture_session()
        sess.add_all([edge_1, edge_2])
        sess.commit()

        near_edges = (
            sess.query(Edge).filter(Edge.start.near(Point(1, 1), 1)).all()
        )

        assert edge_1 not in near_edges
        assert edge_2 in near_edges

        near_edges = (
            sess.query(Edge).filter(Edge.start.near(Point(0, 1), 1)).all()
        )

        assert edge_1 in near_edges and edge_2 in near_edges

    def test_order_by(self):
        self._fixture(False)
        Edge = self.classes.Edge
        s = fixture_session()
        self.assert_compile(
            s.query(Edge).order_by(Edge.start, Edge.end),
            "SELECT edge.id AS edge_id, edge.x1 AS edge_x1, "
            "edge.y1 AS edge_y1, edge.x2 AS edge_x2, edge.y2 AS edge_y2 "
            "FROM edge ORDER BY edge.x1, edge.y1, edge.x2, edge.y2",
        )

    def test_order_by_aliased(self):
        self._fixture(False)
        Edge = self.classes.Edge
        s = fixture_session()
        ea = aliased(Edge)
        self.assert_compile(
            s.query(ea).order_by(ea.start, ea.end),
            "SELECT edge_1.id AS edge_1_id, edge_1.x1 AS edge_1_x1, "
            "edge_1.y1 AS edge_1_y1, edge_1.x2 AS edge_1_x2, "
            "edge_1.y2 AS edge_1_y2 "
            "FROM edge AS edge_1 ORDER BY edge_1.x1, edge_1.y1, "
            "edge_1.x2, edge_1.y2",
        )

    def test_clause_expansion(self):
        self._fixture(False)
        Edge = self.classes.Edge
        from sqlalchemy.orm import configure_mappers

        configure_mappers()

        self.assert_compile(
            select(Edge).order_by(Edge.start),
            "SELECT edge.id, edge.x1, edge.y1, edge.x2, edge.y2 FROM edge "
            "ORDER BY edge.x1, edge.y1",
        )
