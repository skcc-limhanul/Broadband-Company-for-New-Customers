import pandas as pd
import sqlite3
from contextlib import closing
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
db_path = BASE_DIR / 'database' / 'broadband_company.db'

# Attach the saved database under the requested SQLite schema name.
with closing(sqlite3.connect(':memory:')) as conn:
    conn.execute('ATTACH DATABASE ? AS broadband_company', (str(db_path),))
    df_subscribers = pd.read_sql_query(
        '''
        SELECT date,
            service,
            service_sub,
            channel,
            subscriber_new AS subscribers
        FROM subscribers_historical
        ''', conn
    )
    
