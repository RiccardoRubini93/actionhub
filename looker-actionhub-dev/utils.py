from datetime import datetime, timedelta
from google.cloud import bigquery

import os
import re


def is_activation_updated(prefix_project, prefix_dataset):
    """
    Checks if the table in activation layer ha been updated today.
    
    :param prefix_project:          Project prefix. Depends on the environment (dev, test, prod)
    :param prefix_dataset:          Dataset prefix. Depends on the environment
    :return                         True if the activation layer was updated, false otherwise     
    """    
    time_now = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
    date_now = time_now.date()
    timedelta_days = os.environ.get("days_check_updates", "0") # timedelta must be 0 in production (current date)
    date_today = date_now - timedelta(days=int(timedelta_days)) # for test purposes maybe we want to allow another date different from the current date
    date_today_str = date_today.strftime("%Y%m%d")
    
    # Create a BQ client to check last updated date
    query = f"""
        SELECT
            MIN(LAST_UPDATE_DATE)
        FROM
            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_curated.TABLES_LAST_UPDATE`
        WHERE
            DATASET_NAME = '{prefix_dataset}clz_c4m_public_activation'
        """
    # Run query and extract result
    df_result = bigquery.Client().query(query).to_dataframe()
    date_last_update_str = df_result.iloc[0,0].strftime("%Y%m%d")

    # Check if tables are NOT updated yet
    if (date_last_update_str < date_today_str):
        return False
    
    return True


def append_f_looker_sent(content_bq, campaign_code, brand, channel, prefix_dataset):
    """
    Inserts the data received from Looker in the F_LOOKER_SENT table
        
    :param content_bq:              Dataframe to insert in the table
    :param campaign_code:           Campaign / segment used
    :param brand:                   Brand code
    :param channel:                 Channel to which the data were sent (Adform, Google Ads...)
    :param prefix_dataset:          Dataset prefix. Depends on the environment
    """    
    time_now = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
    date_now = time_now.date()

    content_bq["SENT_DATE"] = date_now
    content_bq["SENT_DATETIME"] = time_now
    content_bq["CAMPAIGN_CODE"] = campaign_code
    content_bq["BRAND"] = brand
    content_bq["CHANNEL"] = channel
    
    # Reorder columns
    content_bq = content_bq[["SENT_DATE","SENT_DATETIME","CUSTOMER_CODE","CAMPAIGN_CODE","BRAND","CHANNEL","CONTENT_DESC"]]
    content_bq.reset_index(drop=True, inplace=True)

    # Append DataFrame to BQ table
    client = bigquery.Client()
    table_ref = f'{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT'
    job_config = bigquery.LoadJobConfig(create_disposition="CREATE_NEVER", write_disposition="WRITE_APPEND")
    job = client.load_table_from_dataframe(content_bq, table_ref, job_config=job_config)
    job.result()


def normalize_email(email):
    # Step 1: Trim leading and trailing whitespace
    email = email.strip()
    
    # Step 2: Convert the email to lowercase
    email = email.lower()
    
    # Step 3: Define allowed characters for the local part of the email
    # Local part can contain letters, digits, special characters, and periods
    # Domain part can contain letters, digits, hyphens, and periods
    local_part, domain_part = email.split('@')
    
    # Allowed characters in the local part (based on general email standards)
    allowed_local = re.sub(r'[^a-zA-Z0-9.!#$%&\'*+/=?^_`{|}~-]', '', local_part)
    
    # Allowed characters in the domain part (letters, digits, hyphens, periods)
    allowed_domain = re.sub(r'[^a-zA-Z0-9.-]', '', domain_part)
    
    # Remove consecutive dots in the local part
    allowed_local = re.sub(r'\.\.+', '.', allowed_local)
    
    # Remove leading or trailing dots from local and domain parts
    allowed_local = allowed_local.strip('.')
    allowed_domain = allowed_domain.strip('.')
    
    # Reconstruct the email address
    normalized_email = allowed_local + '@' + allowed_domain
    
    return normalized_email
