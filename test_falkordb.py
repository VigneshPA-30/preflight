import os
from redislite.falkordb_client import FalkorDB

def main():
    # Ensure the target directory exists before initializing the embedded server
    db_dir = ".data"
    db_path = os.path.join(db_dir, "falkor.db")
    os.makedirs(db_dir, exist_ok=True)

    # 1. Open the database files at .data/falkor.db
    print(f"Opening database at {db_path}...")
    db = FalkorDB(db_path)

    # 2. Select graph 'A' and create two nodes and one edge
    graph_a = db.select_graph("A")
    
    create_cypher = """
    CREATE (n1:Person {name: 'Alice', role: 'Developer'})
           -[r:KNOWS {since: 2024}]->
           (n2:Person {name: 'Bob', role: 'Designer'})
    RETURN n1, r, n2
    """
    graph_a.query(create_cypher)
    print("Created 2 nodes and 1 edge in Graph 'A'.")

    # 3. Call copy to clone graph 'A' into graph 'Z'
    # Note: .copy('Z') clones the graph on the server and returns the new Graph object
    graph_z = graph_a.copy("Z")
    print("Cloned Graph 'A' into Graph 'Z'.")

    # 4. Query graph 'Z' and print the rows
    # You can use the returned graph_z object directly (or db.select_graph('Z'))
    query_cypher = """
    MATCH (src:Person)-[rel:KNOWS]->(dst:Person)
    RETURN src.name, type(rel), rel.since, dst.name
    """
    result = graph_z.query(query_cypher)

    print("\nows in Graph 'Z':")
    for row in result.result_set:
        # row is a list of the returned values: ['Alice', 'KNOWS', 2024, 'Bob']
        print(row)

    # 5. Delete both graphs with delete()
    print("\nCleaning up graphs...")
    graph_a.delete()
    print("Deleted Graph 'A'.")
    
    graph_z.delete()
    print("Deleted Graph 'Z'.")

if __name__ == "__main__":
    main()
