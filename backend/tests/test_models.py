from app.models import (
    LineageEdge,
    LineageNode,
    LineageNodeType,
    LineageRelation,
    Message,
    ProjectTag,
    Space,
    SpaceKind,
    SpaceLineageGraph,
    User,
)


def test_user_model():
    user = User(uid="user_123", email="steven@example.com", display_name="Steven")
    assert user.uid == "user_123"
    assert user.email == "steven@example.com"
    assert user.display_name == "Steven"


def test_space_with_project_tags():
    tag_stunts = ProjectTag(name="Stunt Unit", slug="stunts", color="#EF4444")
    space = Space(
        name="Bersama Feature",
        kind=SpaceKind.SHARED_SPACE,
        created_by="user_123",
        tags=[
            ProjectTag(name="General", slug="general"),
            tag_stunts,
        ],
    )
    assert space.name == "Bersama Feature"
    assert len(space.tags) == 2
    assert space.tags[1].slug == "stunts"


def test_message_with_tag():
    msg = Message(
        space_id="space_abc",
        sender_uid="user_123",
        content="Testing breakdown for stunts",
        project_tag="stunts",
    )
    assert msg.project_tag == "stunts"
    assert msg.space_id == "space_abc"


def test_lineage_graph_dag_construction():
    src_node = LineageNode(
        id="node_src",
        node_type=LineageNodeType.SOURCE_FILE,
        label="Treatment_Bersama.pdf",
        project_tag="block-a",
        sha256="abc123sha",
    )
    extract_node = LineageNode(
        id="node_ext",
        node_type=LineageNodeType.EXTRACTED_DATA,
        label="Parsed_Chunks.json",
        project_tag="block-a",
    )
    gate_node = LineageNode(
        id="node_gate",
        node_type=LineageNodeType.APPROVAL_GATE,
        label="Sacrificial Aircraft Risk Gate",
        status="approved",
        project_tag="block-a",
    )
    artifact_node = LineageNode(
        id="node_artifact",
        node_type=LineageNodeType.CONTROL_ARTIFACT,
        label="Shoot_Schedule.csv",
        project_tag="block-a",
    )

    edge1 = LineageEdge(
        from_node_id="node_src",
        to_node_id="node_ext",
        relation=LineageRelation.EXTRACTED_BY,
    )
    edge2 = LineageEdge(
        from_node_id="node_ext",
        to_node_id="node_gate",
        relation=LineageRelation.GATED_BY,
    )
    edge3 = LineageEdge(
        from_node_id="node_gate",
        to_node_id="node_artifact",
        relation=LineageRelation.GENERATED_ARTIFACT,
    )

    graph = SpaceLineageGraph(
        space_id="space_abc",
        project_tag="block-a",
        nodes=[src_node, extract_node, gate_node, artifact_node],
        edges=[edge1, edge2, edge3],
    )

    assert len(graph.nodes) == 4
    assert len(graph.edges) == 3
    assert graph.edges[0].relation == LineageRelation.EXTRACTED_BY
