# Import libs
import json
import pandas as pd
import requests


class AdformSession():
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.__access_token = self.__get_access_token()

    def __get_access_token(self):
        """
        Get access token from Adform API fro generating report

        :return response: Access token
        """

        url = "https://id.adform.com/sts/connect/token"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        body = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://api.adform.com/scope/dmp.segments https://api.adform.com/scope/dmp.segments.readonly https://api.adform.com/scope/dmp.categories https://api.adform.com/scope/dmp.reports.readonly https://api.adform.com/scope/dmp.categories.readonly https://api.adform.com/scope/dmp.accountpermissions https://api.adform.com/scope/dmp.accountpermissions.readonly"
            }
        
        response = requests.post(url, headers=headers, data=body)
        access_token = response.json()["access_token"]
        return access_token

    def search_segment(self, search_string:str):
        """
        Search for a segment in the DMP starting from its name as <search_string>. 

        A list of items containing the segments details is returned
        """

        url = f"https://api.adform.com/v1/dmp/segments?search={search_string}"
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {self.__access_token}'
            }

        response = requests.get(url, headers=headers)
        segments = response.json()
        for segment in segments:
          if segment["refId"] == search_string:
            return segment
            
        return segments

    def create_segment(self, 
        DataProviderId: int,
        CategoryId: int,
        RefId: str,
        Ttl: int,
        Name: str,
        Fee: int,
        Frequency: int,
        Status = "active"):
        """
        Creates a segment for a defined "provider" within the Adfrom DMP. 
        
        If the request is successful, a list with all the details of the created segment is returned.
        """

        url = "https://api.adform.com/v1/dmp/segments"
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.__access_token}'
            }
        body = {
            "DataProviderId": DataProviderId,
            "Status": Status,
            "CategoryId": CategoryId,
            "RefId": RefId,
            "Ttl": Ttl,
            "Name": Name,
            "Fee": Fee,
            "Frequency": Frequency
            }

        response = requests.post(url, headers=headers, json=body)
        return response.json()
