from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "비밀번호"))

def get_all_nodes():
    with driver.session() as session:
        result = session.run("MATCH (n) RETURN n")
        return [record["n"] for record in result]

nodes = get_all_nodes()
for node in nodes:
    print(node)