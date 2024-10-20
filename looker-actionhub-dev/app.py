# Import libraries needed

import json
import os
from datetime import datetime, timedelta

import googleapiclient.discovery
import google.auth
import google.auth.exceptions
import pandas as pd
import pysftp
from flask import Flask, jsonify, request
from google.cloud import logging, bigquery
from waitress import serve
from google.oauth2 import service_account
import googleapiclient.discovery
import s3fs
import boto3
from adform import AdformSession
from google_ads import GoogleAdsSession

from utils import is_activation_updated, append_f_looker_sent

logging_client = logging.Client()

log_name = 'looker-actionhub'
logger = logging_client.logger(log_name)

def get_project_id():
    """Find the GCP project ID when running on Cloud Run."""
    try:
        _, project_id = google.auth.default()
    except google.auth.exceptions.DefaultCredentialsError:
        # Probably running a local development server.
        project_id = os.environ.get('GOOGLE_CLOUD_PROJECT', 'development')

    return project_id

def get_service_url():
    """Return the URL for this service, depending on the environment.

    For local development, this will be http://localhost:8080/. On Cloud Run
    this is https://{service}-{hash}-{region}.a.run.app.
    """
    # https://cloud.google.com/run/docs/reference/rest/v1/namespaces.services/list
    try:
        service = googleapiclient.discovery.build('run', 'v1')
    except google.auth.exceptions.DefaultCredentialsError:
        # Probably running the local development server.
        port = os.environ.get('PORT', '8080')
        url = f'http://localhost:{port}'
    else:
        # https://cloud.google.com/run/docs/reference/container-contract
        k_service = os.environ['K_SERVICE']
        project_id = get_project_id()
        parent = f'namespaces/{project_id}'

        # The global end-point only supports list methods, so you can't use
        # namespaces.services/get unless you know what region to use.
        request = service.namespaces().services().list(parent=parent)
        response = request.execute()

        for item in response['items']:
            if item['metadata']['name'] == k_service:
                url = item['status']['url']
                break
        else:
            raise EnvironmentError('Cannot determine service URL')

    return url



cnopts = pysftp.CnOpts()
cnopts.hostkeys = None

my_api = Flask(__name__)


# ENDPOINT 2: execute
@my_api.route('/sftp_upload/execute', methods=['POST'])
def sendfile():
    
    # Extract current date and time UTC+1
    time_now = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
    time_now_str = time_now.strftime("%Y%m%d_%H%M%S")
    date_now = time_now.date()
    date_now_str = date_now.strftime("%Y%m%d")
    timedelta_days = os.environ.get("days_check_updates", "0") # timedelta must be 0 in production (current date)
    date_today = date_now - timedelta(days=int(timedelta_days)) # for test purposes maybe we want to allow another date different from the current date
    date_today_str = date_today.strftime("%Y%m%d")

    # Data for BQ auth
    project_id = get_project_id()
    if "dev-" in project_id: 
        prefix_project = "dev-"
        prefix_dataset = "dev_"
    elif "test-" in project_id: 
        prefix_project = "test-"
        prefix_dataset = "test_"
    else: 
        prefix_project = "prod-"
        prefix_dataset = ""

    logger.log_text("Checking if all tables in activation layer are updated...", severity='DEFAULT')
    
    client = bigquery.Client()
    query = f"""
        SELECT MIN(LAST_UPDATE_DATE)
        FROM `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_curated.TABLES_LAST_UPDATE`
        WHERE DATASET_NAME = '{prefix_dataset}clz_c4m_public_activation'
    """
    # Run query and extract result
    df_result = client.query(query).to_dataframe()
    date_last_update_str = df_result.iloc[0,0].strftime("%Y%m%d")

    # Check if activation tables are NOT updated yet
    if (date_last_update_str < date_today_str):
        error_message = f"Action NOT performed, tables were updated on {date_last_update_str} and min date allowed is {date_today_str}!"
        logger.log_text(error_message, severity='WARNING')
        message = jsonify({
                "looker": {
                    "success": False,
                    "message": error_message
                    }
            })
        return message

    logger.log_text("Tables are updated! Checking if action already perfomed today...", severity='DEFAULT')

    # Obtain Looker request
    request_json = request.get_json()

    # Extract data from the form
    brand = request_json["form_params"].get("brand", "")
    dataset_id = request_json["form_params"].get("dataset_id", "")
    table_id = request_json["form_params"].get("table_id", "")
    path_sftp = request_json["form_params"]["path_sftp"]

    # Extract connection data from the env variables
    host = os.environ.get("sfmcSftpHost", "ftp.s7.exacttarget.com")
    port_sftp = os.environ.get("sfmcSftpPort", "22")

   # Extract data that depend on the brand from env variables 
    user = ""
    password = ""

    match brand:
        case "CLZ":
            user = os.environ.get("sfmcSftpUsernameClz", "NOT FOUND")
            password = os.environ.get("sfmc_sftp_password_clz", "NOT FOUND")
        case "INT":
            user = os.environ.get("sfmcSftpUsernameInt", "NOT FOUND")
            password = os.environ.get("sfmc_sftp_password_int", "NOT FOUND")
        case "TEZ":
            user = os.environ.get("sfmcSftpUsernameTez", "NOT FOUND")
            password = os.environ.get("sfmc_sftp_password_tez", "NOT FOUND")
        case "FAL":
            user = os.environ.get("sfmcSftpUsernameFal", "NOT FOUND")
            password = os.environ.get("sfmc_sftp_password_fal", "NOT FOUND")

    # Extract desired data form the request  
    url_download = request_json["scheduled_plan"]["download_url"]
    report_name = request_json["scheduled_plan"]["title"]

    send_to_bq = ((dataset_id != "") & (table_id != ""))
    
    # Generate filename from extracted date and time
    file_name = f"{report_name}_{time_now_str}.csv"

    # Define not supported chars you want to replace for the filename
    chars_to_replace = ["/","\\",":","*","?","\"","<",">","|"]
    for char in chars_to_replace:
        file_name = file_name.replace(char, " ")

    # Create a BQ connection if needed
    if send_to_bq:
        client = bigquery.Client()
        table_ref = f'{dataset_id}.{table_id}'
        job_config = bigquery.LoadJobConfig(create_disposition="CREATE_NEVER",
                                            write_disposition="WRITE_APPEND")

    header=True
    success = False
    is_file_created = False
    with pysftp.Connection(host=host, username=user, password=password, port=int(port_sftp), cnopts=cnopts) as sftp:
        success = True  # Connection succeeded
        with sftp.cd(path_sftp):    # Temporally select directory to load files
            with pd.read_csv(url_download, dtype=str, index_col=0, chunksize=100000) as content_df: # Read a chunk from URL
                chunk_number = 1
                for chunk in content_df:
                    # Alert if csv is empty  
                    if chunk_number == 1 and chunk.empty:   
                        logger.log_text(f'sftp_upload - CSV received is empty', severity='WARNING')
                    if is_file_created == False:
                        # Check if action already performed today
                        # Get brand_code & campaign_code from Looker table
                        # brand_code = chunk["Brand"].unique()[0]
                        campaign_code = chunk["CampaignID"].unique()[0]
                        # Query to get the last date action was perfomed
                        query = f"""
                        SELECT
                            MAX(SENT_DATE)
                        FROM
                            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT`
                        WHERE
                            CHANNEL = "MKT"
                            AND BRAND = '{brand}'
                            AND CAMPAIGN_CODE = '{campaign_code}'
                        """
                        # Run query and extract result as string 
                        # We need an exception to handle when the segment doesn't exit and a NaN value is returned
                        df_result = client.query(query).to_dataframe()
                        try:
                            date_last_update_str = df_result.iloc[0,0].strftime("%Y%m%d")
                        except ValueError:
                            date_last_update_str = "00000000"

                        # If the action was already performed today, we can abort the program execution
                        if (date_last_update_str == date_now_str):
                            error_message = f"Last day the action was performed = {date_last_update_str}. No need to run the action again, aborting program..."
                            logger.log_text(error_message, severity='DEFAULT')
                            message = jsonify({
                                "looker": {
                                    "success": True
                                    }
                            })
                            return message
                        
                        logger.log_text("Action NOT performed today. Running action...", severity='DEFAULT')
                        f = sftp.open(file_name,'a') # Open or create the file in server
                        is_file_created = True
                        logger.log_text(f"{file_name} created on SFTP server!", severity='DEFAULT')

                    f.write(chunk.to_csv(index=False, header=header)) # Write the content in CSV file
                    header = False

                    # ======== Send Data to BQ ========
                    if send_to_bq:
                        try:
                            # Build DataFrame
                            content_bq = chunk.copy()
                            content_bq.where(pd.notnull(content_bq), None, inplace=True)
                            content_bq['CONTENT_DESC'] = pd.Series(content_bq.to_dict(orient="records"), index=content_bq.index)#.astype(str)
                            content_bq['CONTENT_DESC'] = content_bq['CONTENT_DESC'].apply(lambda x: json.dumps(x))
                            content_bq['BRAND'] = brand #pd.Series(content_bq['Brand'], index=content_bq.index)
                            content_bq.insert(0, 'CHANNEL', "MKT")
                            content_bq.insert(0, 'SENT_DATE', date_now)
                            content_bq.insert(0, 'SENT_DATETIME', time_now)
                            content_bq = content_bq.rename(columns={"HerokuID": "CUSTOMER_CODE", "CampaignID": "CAMPAIGN_CODE"})
                            content_bq = content_bq[["SENT_DATE","SENT_DATETIME","CUSTOMER_CODE","CAMPAIGN_CODE","BRAND","CHANNEL","CONTENT_DESC"]]
                            content_bq.reset_index(drop=True, inplace=True)
                            # Import to BQ
                            job = client.load_table_from_dataframe(content_bq, table_ref, job_config=job_config)
                            job.result() # Wait to finish the job
                        except Exception as e:
                            send_to_bq = False
                            success = False
                            error_message = str(e)
                            logger.log_text(error_message, severity='ERROR') 
                    # ==============================  
                if is_file_created:
                    f.close()
                    logger.log_text(f"{file_name} closed!", severity='DEFAULT')

    # Generate message
    if success:
        message = jsonify({
            "looker": {
                "success": success
            }
        })
    else:
        message = jsonify({
            "looker": {
                "success": success,
                "message": error_message
                }
        })

    return message

# ENDPOINT 3: execute Adform
@my_api.route('/adform_upload/execute', methods=['POST'])
def sendfile_adform():

    # Extract current date and time UTC+1
    time_now = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
    time_now_str = time_now.strftime("%H%M%S")
    date_now = time_now.date()
    date_now_str = date_now.strftime("%Y%m%d")
    timedelta_days = os.environ.get("days_check_updates", "0") # timedelta must be 0 in production (current date)
    date_today = date_now - timedelta(days=int(timedelta_days)) # for test purposes maybe we want to allow another date different from the current date
    date_today_str = date_today.strftime("%Y%m%d")

    # Data for BQ auth
    project_id = get_project_id()
    if "dev-" in project_id: 
        prefix_project = "dev-"
        prefix_dataset = "dev_"
    elif "test-" in project_id: 
        prefix_project = "test-"
        prefix_dataset = "test_"
    else: 
        prefix_project = "prod-"
        prefix_dataset = ""

    logger.log_text(f"Checking if all tables in activation layer are updated...", severity='DEFAULT')
    # Create a BQ client to check last updated date
    client = bigquery.Client()
    query = f"""
        SELECT
            MIN(LAST_UPDATE_DATE)
        FROM
            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_curated.TABLES_LAST_UPDATE`
        WHERE
            DATASET_NAME = '{prefix_dataset}clz_c4m_public_activation'
        """
    # Run query and extract result
    df_result = client.query(query).to_dataframe()
    date_last_update_str = df_result.iloc[0,0].strftime("%Y%m%d")

    # Check if tables are NOT updated yet
    if (date_last_update_str < date_today_str):
        error_message = f"Action NOT performed, tables were updated on {date_last_update_str} and min date allowed is {date_today_str}!"
        logger.log_text(error_message, severity='WARNING')
        message = jsonify({
                "looker": {
                    "success": False,
                    "message": error_message
                    }
            })
        return message
    
    else:
        logger.log_text(f"Tables are updated! Checking if action is already performed today...", severity='DEFAULT')

        # Obtain response from Looker
        request_json = request.get_json()

        # Extract data from ENV Variables
        access_key = os.environ.get("adform_aws_access_key", "NOT FOUND")
        secret_key = os.environ.get("adform_aws_secret_key", "NOT FOUND")
        client_id = os.environ.get("adform_client_id", "NOT FOUND")
        client_secret = os.environ.get("adform_client_secret", "NOT FOUND")

        # Extract form varianles 
        segment_name = request_json["form_params"]["segment_name"]
        brand = request_json["form_params"].get("brand", "")

        # Extract data from Looker
        url_download = request_json["scheduled_plan"]["download_url"]

        # Read CSV (only 2 columns)
        column_names = ["HerokuID"]
        #logger.log_text(url_download, severity='ERROR') 
        df_looker = pd.read_csv(url_download, usecols=column_names)
        if df_looker.empty:
            logger.log_text(f"CSV received is empty", severity='WARNING')

        # Rename columns
        df_looker = df_looker.rename(columns={
            column_names[0]:"CUSTOMER_CODE"
        })
        df_looker["CUSTOMER_CODE"] = df_looker["CUSTOMER_CODE"].apply(str)

        # Query to get the last date the segment was updated
        query = f"""
        SELECT
            MAX(SENT_DATE)
        FROM
            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT`
        WHERE
            CHANNEL = "ADFORM"
            AND BRAND = '{brand}'
            AND CAMPAIGN_CODE = '{segment_name}'
        """
        # Run query and extract result as string 
        # We need an exception to handle when the segment doesn't exit and a NaN value is returned
        df_result = client.query(query).to_dataframe()
        try:
            date_last_update_str = df_result.iloc[0,0].strftime("%Y%m%d")
        except ValueError:
            date_last_update_str = "00000000"

        # If the segment was already updated today, we can abort the program execution
        if (date_last_update_str == date_now_str):
            error_message = f"Last day the segment was updated = {date_last_update_str}. No need to run the action again, aborting program..."
            logger.log_text(error_message, severity='DEFAULT')
            message = jsonify({
                "looker": {
                    "success": True
                    }
            })
            return message

        logger.log_text(f"Running the action...", severity='DEFAULT')

        # Connect to AdForm API
        adform_session = AdformSession(client_id, client_secret)

        # Check if segment already exists
        segment = adform_session.search_segment(search_string=segment_name)
        
        # Check if segment doesn't exist (empty list)
        if len(segment) == 0:
            # Define values to create segment
            DataProviderId = os.environ.get("DataProviderId")
            CategoryId = request_json["form_params"].get("category_id","")
            if ((CategoryId == "") | (CategoryId.isdigit()==False)): 
                CategoryId = os.environ.get("CategoryId")
            RefId = segment_name
            Ttl = request_json["form_params"].get("ttl","")
            if ((Ttl == "") | (Ttl.isdigit()==False)): 
                Ttl = os.environ.get("Ttl")
            if (int(Ttl) < 1):
                Ttl = os.environ.get("Ttl")
            if (int(Ttl) > 120):
                Ttl = "120"
            Name = segment_name
            Fee = os.environ.get("Fee")
            Frequency = request_json["form_params"].get("frequency","")
            if ((Frequency == "") | (Frequency.isdigit()==False)): 
                Frequency = os.environ.get("Frequency")
            if (int(Frequency) < 1):
                Frequency = os.environ.get("Frequency")
            Status = os.environ.get("Status")
            # Create Segment
            segment = adform_session.create_segment(
               int(DataProviderId), 
               int(CategoryId), 
               RefId, 
               int(Ttl), 
               Name, 
               int(Fee), 
               int(Frequency), 
               Status
            )

            logger.log_text(f"NEW SEGMENT CREATED => {segment}", severity='DEFAULT') 
        else:
            logger.log_text(f"SEGMENT ALREADY EXISTS => {segment}", severity='DEFAULT')
        
        # Get segment ID
        segment_refId = segment["refId"]
        report_name = f"{segment_refId}_{time_now_str}.csv"

        # Define not supported chars you want to replace for the report_name
        chars_to_replace = [" ","/","\\",":","*","?","\"","<",">","|"]
        for char in chars_to_replace:
            report_name = report_name.replace(char, "-")

        
        # Add data and format dataframe to upload to BQ
        df_looker["SENT_DATE"] = date_now
        df_looker["SENT_DATETIME"] = time_now
        df_looker["CAMPAIGN_CODE"] = segment_refId
        df_looker["BRAND"] = brand
        df_looker["CHANNEL"] = "ADFORM"
        df_looker["CONTENT_DESC"] = ""
        # Reorder columns
        df_looker = df_looker[["SENT_DATE","SENT_DATETIME","CUSTOMER_CODE","CAMPAIGN_CODE","BRAND","CHANNEL","CONTENT_DESC"]]

        # Append DataFrame to BQ table
        client = bigquery.Client()
        table_ref = f'{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT'
        job_config = bigquery.LoadJobConfig(create_disposition="CREATE_NEVER",
                                            write_disposition="WRITE_APPEND")

        # Upload table to BQ
        job = client.load_table_from_dataframe(df_looker, table_ref, job_config=job_config)
        job.result()

        # JOIN query to extract EXTERNAL_CODE
        # This query is build like this because we can not pass the array of customer codes diectly, as we can run in an error because of too long query 
        query = f"""
        SELECT
            DISTINCT(EXTERNAL_CODE),
            CAMPAIGN_CODE
        FROM
            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_normalized.M_MEDIA_KNOWN_IDENTITY` t1
        INNER JOIN
            `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT` t2
        ON
            t1.CUSTOMER_CODE=TO_BASE64(SHA256(CAST(t2.CUSTOMER_CODE AS STRING)))
        WHERE
            CHANNEL="ADFORM"
            AND SENT_DATE = '{date_now}'
            AND CAMPAIGN_CODE = '{segment_refId}'
        """

        # Run query and save as DataFrame
        df_result = client.query(query).to_dataframe()
        success = False
        
        # Upload Dataframe to S3 bucket
        try:
            # Create an S3 Client
            s3 = s3fs.S3FileSystem(key=access_key, secret=secret_key)
            ProviderTitle = os.environ.get("ProviderTitle")
            file_path = f's3://data-providers/{ProviderTitle}/dt={date_now_str}/{report_name}'
            # Create file and save data as CSV
            with s3.open(file_path, 'w') as f:
                df_result.to_csv(f, index=False, header=False, sep ='\t')
                success = True
                # Print Logs
                logger.log_text(f"FILE UPLOADED TO S3 => {file_path}", severity='DEFAULT')

        except Exception as e:
            success = False
            error_message = str(e)
            logger.log_text(f"ERROR => {error_message}", severity='ERROR') 
        

        # Generate message to return
        if success:
            message = jsonify({
                "looker": {
                    "success": success
                }
            })
        else:
            message = jsonify({
                "looker": {
                    "success": success,
                    "message": error_message
                    }
            })

        return message


@my_api.route('/googleads_upload/execute', methods=['POST'])
def sendfile_googleads():
    """
    Action that adds and removes users form a Google Ads user list. 
    It also creates the user list if not exists.
    """

    logger.log_text(f"Executing Google Ads action", severity='INFO') 

    # Dinamically get the dataset  prefix depending on the project
    project_id = get_project_id()
    if "dev-" in project_id: 
        prefix_project = "dev-"
        prefix_dataset = "dev_"
    elif "test-" in project_id: 
        prefix_project = "test-"
        prefix_dataset = "test_"
    else: 
        prefix_project = "prod-"
        prefix_dataset = ""
        
    logger.log_text(f"Checking if all tables in activation layer are updated...", severity='DEFAULT')
    
    is_updated = is_activation_updated(prefix_project, prefix_dataset)
    
    # Check if tables are NOT updated yet
    if (not is_updated):
        error_message = f"Action NOT performed, tables were NOT updated!"
        logger.log_text(error_message, severity = 'WARNING')
        message = jsonify({
                "looker": {
                    "success": False,
                    "message": error_message
                    }
            })
        
        return message

    # Extract form varianles 
    request_json = request.get_json()
    segment_name = request_json["form_params"]["segment_name"]
    brand = request_json["form_params"].get("brand", "")
    country = request_json["form_params"].get("country", "")
    ttl = request_json["form_params"].get("ttl", "10")

    # column_names = ["Email", "PhoneNumber", "country", "customer_code"] 
   # df_looker = pd.read_csv(url_download, usecols=column_names, dtype=dtype, keep_default_na=False)
   # df_looker.fillna('', inplace=True)
    
    # Connect to GoogleAds API
    googleads_session = GoogleAdsSession(brand, country)
    
    # Check if segment already exists, if not a new one is created
    segment_id = googleads_session.search_segment(segment_name = segment_name)

    if segment_id is None:

        if ttl == "" or ttl.isdigit() == False or int(ttl)< 1: 
            ttl = os.environ.get("Ttl")
        if int(ttl) > 120:
            ttl = 120
        
        segment_creation_result = googleads_session.create_segment(segment_name = segment_name, description="", ttl = int(ttl))
        segment_id = segment_creation_result.results[0].resource_name.split("/")[3] 

    # Extract data from Looker
    url_download = request.get_json()["scheduled_plan"]["download_url"]

    success = True

    try:
        # Read a chunk from URL
        with pd.read_csv(url_download, dtype=str, index_col=0, chunksize=100000, keep_default_na=False) as content_df:
            
            chunk_number = 1
            job_resource_name = googleads_session.create_offline_user_data_job_service(segment_id = segment_id)
            
            for chunk in content_df:
                # Alert if csv is empty  
                if chunk_number == 1 and chunk.empty:   
                    logger.log_text(f'sftp_upload - CSV received is empty', severity='WARNING')
                else:
                    if(job_resource_name is not None):
                        success = googleads_session.upsert_user_in_segment(users_to_add = chunk, job_resource_name = job_resource_name)
                        
                        content_bq = chunk.copy()
                        
                        # Add the country so it is included in the CONTENT_DESC json
                        content_bq['country'] = country
                        content_bq.where(pd.notnull(content_bq), None, inplace=True)
                        content_bq['CONTENT_DESC'] = pd.Series(content_bq.to_dict(orient="records"), index=content_bq.index)
                        content_bq['CONTENT_DESC'] = content_bq['CONTENT_DESC'].apply(lambda x: json.dumps(x))
                        content_bq = content_bq.rename(columns={"HerokuID": "CUSTOMER_CODE"})

                        # Remove the country column because it will not be inserted its own column
                        content_bq = content_bq.drop(columns=['country'])

                        append_f_looker_sent(content_bq, segment_name, brand, "GOOGLEADS", prefix_dataset)
                        
            if(job_resource_name is not None):
                googleads_session.run_offline_user_data_job(job_resource_name = job_resource_name)
            else:
                logger.log_text(f"ERROR => No job was created", severity='ERROR') 
                success = False

        # Query the users that exist in the segment because they where inserted before
        # and Looker did not sent this time, in order to delte them
        date_now = datetime.today().strftime('%Y-%m-%d') 
        yesterday = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')

        query = f"""
            SELECT DISTINCT
                JSON_EXTRACT_SCALAR(LS1.CONTENT_DESC, '$.Email') AS email,
                JSON_EXTRACT_SCALAR(LS1.CONTENT_DESC, '$.PhoneNumber') AS phone_number
            FROM `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT` LS1
            WHERE
                CHANNEL = "GOOGLEADS"
                AND LS1.SENT_DATE = '{yesterday}'
                AND LS1.CAMPAIGN_CODE = '{segment_name}'
                AND LS1.BRAND = '{brand}'
                AND JSON_EXTRACT_SCALAR(LS1.CONTENT_DESC, '$.country') = '{country}'
                AND LS1.CUSTOMER_CODE NOT IN (
                    SELECT
                        LS2.CUSTOMER_CODE
                    FROM `{prefix_project}cross-cloud4marketing.{prefix_dataset}clz_c4m_public_activation.F_LOOKER_SENT` LS2
                    WHERE 
                        LS2.CHANNEL = 'GOOGLEADS'
                        AND LS2.SENT_DATE = '{date_now}'
                        AND LS2.CAMPAIGN_CODE = '{segment_name}'
                        AND LS2.BRAND = '{brand}'
                        AND JSON_EXTRACT_SCALAR(LS2.CONTENT_DESC, '$.country') = '{country}'
                )
        """
        
        df_users_to_remove = bigquery.Client().query(query).to_dataframe()
        
        if(not df_users_to_remove.empty):
            logger.log_text(f"Removing users from segment", severity='INFO') 

            # Set the maximum size of each chunk. The API only allows 100k identifiers per request
            chunk_size = 50000  
            num_rows = len(df_users_to_remove)

            # The same job cannot be used for create and remove operations so a new one is created 
            job_resource_name_remove = googleads_session.create_offline_user_data_job_service(segment_id = segment_id)
            
            # Process the DataFrame in chunks of 50.000 rows
            for start_row in range(0, num_rows, chunk_size):
                chunk = df_users_to_remove[start_row:start_row + chunk_size]
                success = googleads_session.remove_user_in_segment(users_to_remove = chunk, job_resource_name = job_resource_name_remove)
            
            googleads_session.run_offline_user_data_job(job_resource_name = job_resource_name_remove)
            
        else:
            logger.log_text(f"No users need to be removed from segment", severity='INFO') 

    except Exception as e:
        logger.log_text(f"ERROR => There was an error processing Looker request", severity='WARNING') 
        logger.log_text(f"{str(e)}", severity='WARNING') 


    return jsonify({
        "looker": {
            "success": success
        }
    })


#####################################################
#################### FORMS START ####################
#####################################################


# Returns the json specification of the SFMC action form
@my_api.route('/sftp_upload/form', methods=['POST'])
def formdesign_sfmc():

    with open('forms/form_sfmc.json') as json_file:
        form = json.load(json_file)

    return jsonify(form)


# Returns the json specification of the Adform action form
@my_api.route('/adform_upload/form', methods=['POST'])
def formdesign_adform():

    with open('forms/form_adform.json') as json_file:
        form = json.load(json_file)

    default_categoy_id = os.environ.get("CategoryId")
    default_frecuency = os.environ.get("Frequency")
    default_ttl = os.environ.get("Ttl")
    
    form[1]["description"] = f"Category  (default={default_categoy_id})"
    form[2]["description"] = f"Frequency (default={default_frecuency})"
    form[3]["description"] = f"TTL (default={default_ttl})"

    return jsonify(form)


# Returns the json specification of the Google Ads action form
@my_api.route('/googleads_upload/form', methods=['POST'])
def formdesign_googleads():

    with open('forms/form_googleads.json') as json_file:
        form = json.load(json_file)

    default_ttl = os.environ.get("Ttl")
    form[2]["description"] = f"TTL (default={default_ttl})"

    return jsonify(form)


#####################################################
##################### FORMS END #####################
#####################################################



# Lists the available actions in the custom Action Hub. 
# The configuration is added dinamically based on the environment (dev/test/prod)
@my_api.route('/list', methods=['POST'])
def returnjson():
    service_url = get_service_url()
    # Get project_id and write corresponding prefeix to action
    project_id = get_project_id()
    prefix=""
    if "dev-" in project_id: prefix = "DEV - "
    elif "test-" in project_id: prefix = "TEST - "
    else: pass

    with open('config/integrations.json') as json_file:
        integrations = json.load(json_file)
    
    integrations[0]["label"] = prefix + integrations[0]["label"]
    integrations[0]["url"] = f"{service_url}/sftp_upload/execute"
    integrations[0]["form_url"] = f"{service_url}/sftp_upload/form"

    integrations[1]["label"] = prefix + integrations[1]["label"]
    integrations[1]["url"] = f"{service_url}/adform_upload/execute"
    integrations[1]["form_url"] = f"{service_url}/adform_upload/form"

    integrations[2]["label"] = prefix + integrations[2]["label"]
    integrations[2]["url"] = f"{service_url}/googleads_upload/execute"
    integrations[2]["form_url"] = f"{service_url}/googleads_upload/form"

    return jsonify({
        "label": "Calzedonia Custom Actions",
        "integrations": integrations})


if __name__ == '__main__':
    server_port = os.environ.get('PORT', '8080')
    serve(my_api, port=server_port, host='0.0.0.0')

