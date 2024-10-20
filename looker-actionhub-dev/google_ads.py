import hashlib
import json
import os

from google.cloud import logging

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


logger = logging.Client().logger('looker-actionhub')


class GoogleAdsSession():
    def __init__(self, brand, country):
        
        os.environ["GOOGLE_ADS_USE_PROTO_PLUS"] = "False"
        
        # Credentials are loaded form the environment variables
        self.client = GoogleAdsClient.load_from_env()
    
        with open('config/google_ads_customers.json') as json_file:
            google_ads_customers = json.load(json_file)
        
        if(brand in google_ads_customers and country in google_ads_customers[brand]):
           self.customer_id = google_ads_customers[brand][country]
        else:
            error_message = f"No Google Ads account for brand {brand} and country {country}"
            logger.log_text(error_message, severity='ERROR')
            raise Exception(error_message)



    def search_segment(self, segment_name):
        """
        Segment search by name in Google Ads

        :param self:                Instance of the class
        :param segment_name:        Name of the segment to search
        :return segment_id:         Google Ads segment id, None if the segment does not exist
        """
        ga_service = self.client.get_service("GoogleAdsService")

        # Construct the query to search for the user list by name
        query = f"""
            SELECT
                user_list.id
            FROM
                user_list
            WHERE
                user_list.name = '{segment_name}'
        """

        try:
            # Execute the query
            response = ga_service.search(customer_id = self.customer_id, query = query)

            isEmpty = True
            segment_id = None
            
            for row in response:
                isEmpty = False
                segment_id = row.user_list.id

            if not isEmpty:
                logger.log_text(f"SEGMENT ALREADY EXISTS => {segment_name} : {segment_id} ", severity='DEFAULT')
                return segment_id
            else:
                logger.log_text(f"SEGMENT DOES NOT EXIST => {segment_name}", severity='DEFAULT')                
                return None

        except GoogleAdsException as ex:
            logger.log_text(f"ERROR => Request search_segment failed with status: {ex.error.code().name}. Error message: {ex.error.message}",  severity="ERROR")
            for error in ex.failure.errors:
                logger.log_text(f"Error: {error.error_code} - {error.message}", severity="ERROR")
            raise


    def create_segment(self, segment_name, description, ttl):
        """
        Segment creation in Google Ads

        :param self:                Instance of the class
        :param segment_name:        Name of the segment to create
        :param description:         Description of the segment to create
        :param ttl:                 Membership duration of the segment to create
        :return response:           Google Ad response with the segment information
        """
        
        try:
            user_list_service = self.client.get_service("UserListService")
            user_list_operation = self.client.get_type("UserListOperation")
            user_list = user_list_operation.create
            user_list.name = segment_name
            user_list.description = description
            user_list.membership_status = self.client.enums.UserListMembershipStatusEnum.OPEN
            user_list.membership_life_span = ttl
            user_list.crm_based_user_list.upload_key_type = self.client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO

            response = user_list_service.mutate_user_lists(
                customer_id = self.customer_id, 
                operations = [user_list_operation]
            )

            logger.log_text(f"NEW SEGMENT CREATED => {segment_name}:{response.results[0].resource_name}", severity='DEFAULT') 

            return response
        
        except GoogleAdsException as ex:
            logger.log_text(f"ERROR => Request create_segment failed with status: {ex.error.code().name}. Error message: {ex.error.message}", severity='ERROR') 
            for error in ex.failure.errors:
                logger.log_text(f"Error: {error.error_code} - {error.message}")
            raise


    def create_offline_user_data_job_service(self, segment_id):
        """
        Creates an OfflineUserDataJobService that can beuser for multiple operations

        :param self:                        Instance of the class
        :param segment_id:                  Id of the segment to which the users are going to be added
        :return                             The OfflineUserDataJobService resource name created 
        """
        try:
            # Step 1: Create an Offline User Data Job 
            offline_user_data_job_service = self.client.get_service("OfflineUserDataJobService")

            offline_user_data_job  = self.client.get_type("OfflineUserDataJob")
            offline_user_data_job.type_ = self.client.enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST

            offline_user_data_job.customer_match_user_list_metadata.user_list = self.client.get_service("UserListService").user_list_path(
                self.customer_id, segment_id
            )

            offline_user_data_job.customer_match_user_list_metadata.consent.ad_user_data = self.client.enums.ConsentStatusEnum.GRANTED
            offline_user_data_job.customer_match_user_list_metadata.consent.ad_personalization = self.client.enums.ConsentStatusEnum.GRANTED

            # Create the job
            response = offline_user_data_job_service.create_offline_user_data_job(
                customer_id = self.customer_id, job=offline_user_data_job
            )
            job_resource_name = response.resource_name

            logger.log_text(f"Created OfflineUserDataJob with resource name {job_resource_name}.")
            
            return job_resource_name
        
        except GoogleAdsException as ex:
            logger.log_text(f"ERROR => Request create_offline_user_data_job_service failed with status: {ex.error.code().name}. Error message: {ex.error}", severity='ERROR') 


    def upsert_user_in_segment(self, users_to_add, job_resource_name):
        """
        Adds users to a segment. If a user that already exists is added again with different data
        and updates is done by Google Ads.

        :param self:                        Instance of the class
        :param users_to_add:                List of users to add to the segment
        :param job_resource_name:           Resource name of the OfflineUserDataJobService to use
        """
        
        logger.log_text(f"Upserting users in segment using the job {job_resource_name}")

        try:
        
            # Prepare the operations to add users to the user list
            operations = [] 
            users_to_add.columns = users_to_add.columns.str.strip()
            
            for index, row in users_to_add.iterrows():
                user_data_job = self.client.get_type("OfflineUserDataJobOperation")
                user_identifier = self.client.get_type("UserIdentifier")
                user_data = self.client.get_type("UserData")

                # Set the user identifiers
                if 'Email' in users_to_add.columns and row['Email'] is not None and row['Email'] != "": 
                    user_identifier.hashed_email = hashlib.sha256(str(row['Email']).strip().lower().encode('utf-8')).hexdigest()

                if 'PhoneNumber' in users_to_add.columns and row['PhoneNumber'] is not None and str(row['PhoneNumber']) != "":
                    user_identifier.hashed_phone_number = hashlib.sha256(str(row['PhoneNumber']).replace("'", "").strip().lower().encode('utf-8')).hexdigest()
    
                user_data.user_identifiers.append(user_identifier)
                user_data_job.create.CopyFrom(user_data)

                operations.append(user_data_job)

            add_offline_user_data_job_operations_request = self.client.get_type("AddOfflineUserDataJobOperationsRequest")
            add_offline_user_data_job_operations_request.resource_name = job_resource_name
            add_offline_user_data_job_operations_request.operations.extend(operations)
            add_offline_user_data_job_operations_request.enable_partial_failure = True
            
            # Creates the OfflineUserDataJobService client.
            offline_user_data_job_service = self.client.get_service("OfflineUserDataJobService")
  
            offline_user_data_job_service.add_offline_user_data_job_operations(add_offline_user_data_job_operations_request)

            logger.log_text(f"Users have been added to job with ID {job_resource_name}.", severity = "INFO")
                 
            return True
        
        except GoogleAdsException as ex:
            logger.log_text(f"ERROR => Request upsert_user_in_segment failed with status: {ex.error.code().name}. Error message: {ex.error}", severity='ERROR') 
            return False


    def remove_user_in_segment(self, users_to_remove, job_resource_name):
        """
        Removes users form a segment.

        :param self:                        Instance of the class
        :param users_to_remove:             List of users to remove from the segment
        :param job_resource_name:           Resource name of the OfflineUserDataJobService to use
        """
        
        logger.log_text(f"Removing users from segment using the job {job_resource_name}")
        
        try:

            operations = []
            for index, row in users_to_remove.iterrows():
                user_data_job = self.client.get_type("OfflineUserDataJobOperation")
                user_identifier = self.client.get_type("UserIdentifier")
                user_data = self.client.get_type("UserData")
                    
                # Set the user identifiers
                if row['email'] is not None and str(row['email']) != "": 
                    user_identifier.hashed_email = hashlib.sha256(str(row['email']).strip().lower().encode('utf-8')).hexdigest()

                if row['phone_number'] is not None and str(row['phone_number']) != "":
                    user_identifier.hashed_phone_number = hashlib.sha256(str(row['phone_number']).replace("'", "").strip().lower().encode('utf-8')).hexdigest()
    
                user_data.user_identifiers.append(user_identifier)
                user_data_job.remove.CopyFrom(user_data)

                operations.append(user_data_job)

            add_offline_user_data_job_operations_request = self.client.get_type("AddOfflineUserDataJobOperationsRequest")
            add_offline_user_data_job_operations_request.resource_name = job_resource_name
            add_offline_user_data_job_operations_request.operations.extend(operations)
            add_offline_user_data_job_operations_request.enable_partial_failure = True
            
            # Creates the OfflineUserDataJobService client.
            offline_user_data_job_service = self.client.get_service("OfflineUserDataJobService")
  
            offline_user_data_job_service.add_offline_user_data_job_operations(add_offline_user_data_job_operations_request)

            logger.log_text(f"Users have been added to job with ID {job_resource_name}.", severity = "INFO")
            
            return True
        
        except GoogleAdsException as ex:
            logger.log_text(f"ERROR => Request remove_user_in_segment failed with status: {ex.error.code().name}. Error message: {ex.error}", severity='ERROR') 
            return False


    def run_offline_user_data_job(self, job_resource_name):
        """
        Sends the request to run the offline user data job.
        
        :param self:                        Instance of the class
        :param job_resource_name:           Resource name of the OfflineUserDataJobService to use
        """
        # Run the job to upload the data.
        run_request = self.client.get_type("RunOfflineUserDataJobRequest")
        run_request.resource_name = job_resource_name

        offline_user_data_job_service = self.client.get_service("OfflineUserDataJobService")
        offline_user_data_job_service.run_offline_user_data_job(run_request)

        logger.log_text(f"Job with ID {job_resource_name} running", severity = "INFO")
        
