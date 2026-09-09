import pandas as pd
import sqlite3
from contextlib import closing
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / 'dataset'

# Promotion table
df_promo = pd.read_excel(
    DATASET_DIR / 'broadband_company_subscribers.xlsx', sheet_name='프로모션정책')
cols = {'프로모션ID': 'promo_id', '정책명': 'promo_title', '서비스': 'service', '서비스상세': 'service_sub', '채널': 'channel', '가입유형': 'subscription_type', '약정개월': 'contract_month', '대상고객': 'target_customer', '결합조건': 'cross_condition', '할인유형': 'discount_type',
        '기본월단가(원)': 'price_base', '월할인액(원)': 'price_discount', '프로모션월단가(원)': 'price_promo', '혜택적용개월': 'discount_month', '정상설치비(원)': 'installation_fee', '적용설치비(원)': 'installation_fee_discount', '고객사은품/지원금(원)': 'discount_customer', '채널인센티브(원)': 'channel_incentive', '최소유지일수': 'minimum_subscription_day', '적용시작일': 'promo_start_at', '적용종료일': 'promo_end_at', '총고객혜택(원)': 'total_discount_customer', '비고': 'remark'}
df_promo.rename(columns=cols, inplace=True)

# Subscriber table (Historical)
df_sub_h = pd.read_excel(DATASET_DIR / 'broadband_company_subscribers.xlsx',
                         sheet_name='개통자수_일별').drop(['기준비중', '일총개통자수', '실제일비중'], axis=1)
cols = {'기준일': 'date', '서비스': 'service', '서비스상세': 'service_sub',
        '채널': 'channel', '활성획득프로모션수': 'active_promo', '신규개통자수': 'subscriber_new'}
df_sub_h.rename(columns=cols, inplace=True)

# Subscriber table (New)
df_sub_n = pd.read_excel(DATASET_DIR / 'subscribers_new.xlsx',
                         ).drop(['기준비중', '일총개통자수', '실제일비중'], axis=1)
df_sub_n.rename(columns=cols, inplace=True)


# Save the DataFrames to SQLite.
db_path = BASE_DIR / 'database' / 'broadband_company.db'
db_path.parent.mkdir(parents=True, exist_ok=True)

with closing(sqlite3.connect(db_path)) as conn:
    df_promo.to_sql('promotion', conn, if_exists='replace', index=False)
    df_sub_h.to_sql('subscribers_historical', conn,
                    if_exists='replace', index=False)
    df_sub_n.to_sql('subscribers_new', conn, if_exists='replace', index=False)
