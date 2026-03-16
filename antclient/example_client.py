#!/usr/bin/env python3
"""Example usage of Anthive Client

This script demonstrates basic usage of the Anthive Client library.
"""

from antclient import AnthiveClient


def main():
    # Connect to API
    client = AnthiveClient("http://localhost:8080")
    print(f"Connected to: {client.base_url}\n")

    # 1. Server Health
    print("=== Server Health ===")
    health = client.get_health()
    print(f"Status: {health['status']}")
    print(f"Databases: {health['databases']}")
    print()

    # 2. List Databases
    print("=== Available Databases ===")
    databases = client.get_databases()
    print(f"Found {len(databases)} databases:")
    for db in databases[:5]:  # Show first 5
        print(f"  - {db['id']}: {db['n_cells']:,} cells, {db['n_genes']:,} genes")
    print()

    if not databases:
        print("No databases available. Exiting.")
        return

    # Use first database for examples
    db_id = databases[0]['id']
    print(f"Using database: {db_id}\n")

    # 3. Database Info
    print("=== Database Information ===")
    info = client.get_database_info(db_id)
    print(f"Title: {info.get('title', 'N/A')}")
    print(f"Cells: {info['n_cells']:,}")
    print(f"Genes: {info['n_genes']:,}")
    print(f"Layers: {info['layers']}")
    print(f"Embeddings: {info['embeddings']}")
    print()

    # 4. Search Genes
    print("=== Gene Search ===")
    genes = client.search_genes(db_id, q="CD4", limit=5)
    print(f"Genes matching 'CD4': {genes}")
    print()

    # 5. Metadata Fields
    print("=== Metadata Fields ===")
    fields = client.get_metadata_fields(db_id)
    print(f"Numerical fields ({len(fields['numerical'])}): {fields['numerical'][:5]}")
    print(f"Categorical fields ({len(fields['categorical'])}): {fields['categorical']}")
    print()

    # 6. Get Cell Data (JSON)
    print("=== Cell Data (JSON) ===")
    if genes:
        result = client.get_cells(
            db_id,
            genes=genes[:2],  # First 2 genes
            metadata=fields['categorical'][:1],  # First categorical field
            limit=3
        )
        print(f"Retrieved {result['n_cells']} cells with {result['n_columns']} columns")
        print(f"Columns: {result['columns']}")
        print(f"First cell: {result['data'][0]}")
        print()

    # 7. Get Cell Data (DataFrame) - requires pandas
    print("=== Cell Data (DataFrame) ===")
    try:
        df = client.get_cells(
            db_id,
            genes=genes[:2] if genes else None,
            metadata=fields['categorical'][:2],  # First 2 categorical fields
            limit=5,
            format='dataframe'
        )
        print(df)
        print()
    except ImportError:
        print("pandas not installed - skipping DataFrame example")
        print("Install with: pip install pandas")
        print()

    # 8. Embeddings (if available)
    if info['embeddings']:
        print(f"=== Embeddings: {info['embeddings'][0]} ===")
        try:
            emb_df = client.get_embedding_data(
                db_id,
                info['embeddings'][0],
                n_dims=2,
                limit=5,
                format='dataframe'
            )
            print(emb_df)
            print()
        except ImportError:
            print("pandas not installed - skipping DataFrame example")
            print()

    # 9. SQL Query
    print("=== SQL Query ===")
    try:
        sql_result = client.execute_sql(
            db_id,
            "SELECT * FROM cells LIMIT 3"
        )
        print(f"Query returned {sql_result['n_rows']} rows, {sql_result['n_columns']} columns")
        print(f"Columns: {sql_result['columns']}")
        print()
    except Exception as e:
        print(f"SQL query failed: {e}")
        print()

    # 10. Performance Metrics (Phase 2)
    print("=== Server Metrics (Phase 2) ===")
    metrics = client.get_metrics()
    print(f"Uptime: {metrics['uptime_seconds']} seconds")
    print(f"Total requests: {metrics['requests']['total']}")
    pool = metrics['connection_pool']
    print(f"Connection pool: {pool['pooled_connections']} pooled, {pool['in_use']} in use")
    print(f"Cache hit rate: {pool['hit_rate']:.2%}")
    print()

    print("=== Examples Complete ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
