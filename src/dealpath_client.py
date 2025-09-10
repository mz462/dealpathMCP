import os
import requests
from dotenv import load_dotenv
from typing import Tuple, Dict, Any
import re

load_dotenv()

DEALPATH_API_KEY = os.getenv("dealpath_key")
BASE_URL = "https://api.dealpath.com"
FILES_BASE_URL = "https://files.dealpath.com"

class DealpathClient:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {DEALPATH_API_KEY}",
            "Accept": "application/vnd.dealpath.api.v1+json",
        }

    def get_deals(self, **filters):
        response = requests.get(f"{BASE_URL}/deals", headers=self.headers, params=filters)
        response.raise_for_status()
        return response.json()

    def get_deal_by_id(self, deal_id: str):
        response = requests.get(f"{BASE_URL}/deals/{deal_id}", headers=self.headers)
        response.raise_for_status()
        return response.json()

    def get_assets(self, **filters):
        response = requests.get(f"{BASE_URL}/assets", headers=self.headers, params=filters)
        response.raise_for_status()
        return response.json()

    def get_deal_files_by_id(self, deal_id: int, **params):
        """
        Returns a paginated list of files belonging to a deal.

        :param deal_id: The ID of the deal.
        :param params: Optional query parameters:
            - parent_folder_ids: list[int]
            - file_tag_definition_ids: list[int]
            - updated_before: int (timestamp)
            - updated_after: int (timestamp)
            - next_token: str
        """
        url = f"{BASE_URL}/files/deal/{deal_id}"
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_field_definitions(self, **params):
        response = requests.get(f"{BASE_URL}/field_definitions", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_deal_id(self, deal_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/deal/{deal_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_investment_id(self, investment_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/investment/{investment_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_property_id(self, property_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/property/{property_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_asset_id(self, asset_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/asset/{asset_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_loan_id(self, loan_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/loan/{loan_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_fields_by_field_definition_id(self, field_definition_id: str, **params):
        response = requests.get(f"{BASE_URL}/fields/field_definition/{field_definition_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_asset_files_by_id(self, asset_id: int, **params):
        url = f"{BASE_URL}/files/asset/{asset_id}"
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_file_by_id(self, file_id: str):
        # Step 1: Get the temporary download URL
        url_response = requests.get(f"{BASE_URL}/file/{file_id}/download_url", headers=self.headers)
        url_response.raise_for_status()
        download_details = url_response.json()
        download_url = download_details.get("url")
        filename = download_details.get("name", f"{file_id}.unknown")

        if not download_url:
            raise Exception("Could not retrieve download URL.")

        # Step 2: Download the file from the temporary URL
        file_response = requests.get(download_url)
        file_response.raise_for_status()

        return {
            "content": file_response.content,
            "filename": filename
        }

    def get_file_download_url(self, file_id: str):
        """Return a temporary, signed download URL and filename for a file.

        This avoids proxying large binaries through our server; callers can
        download directly from Dealpath using the signed URL.
        """
        url_response = requests.get(f"{BASE_URL}/file/{file_id}/download_url", headers=self.headers)
        url_response.raise_for_status()
        data = url_response.json()
        return {"url": data.get("url"), "filename": data.get("name", f"{file_id}")}

    def download_file_content(self, file_id: str) -> Dict[str, Any]:
        """Download file bytes directly from files.dealpath.com.

        Returns dict with keys: content (bytes), filename (str), mime_type (str | None).
        """
        url = f"{FILES_BASE_URL}/file/{file_id}"
        # Use only Authorization header; do not send JSON Accept header here
        headers = {"Authorization": f"Bearer {DEALPATH_API_KEY}"}
        resp = requests.get(url, headers=headers, stream=True)
        resp.raise_for_status()

        # Try to parse filename from Content-Disposition
        filename = None
        cd = resp.headers.get("Content-Disposition")
        if cd:
            # naive extraction: filename="name" or filename=name
            m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
            if m:
                filename = requests.utils.unquote(m.group(1))
            else:
                m2 = re.search(r"filename=\"?([^\";]+)\"?", cd)
                if m2:
                    filename = m2.group(1)
        if not filename:
            filename = str(file_id)

        mime_type = resp.headers.get("Content-Type")
        content = resp.content
        return {"content": content, "filename": filename, "mime_type": mime_type}

    def get_file_tag_definitions(self, **params):
        response = requests.get(f"{BASE_URL}/file_tag_definitions", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_folders_by_deal_id(self, deal_id: int, **params):
        url = f"{BASE_URL}/folders/deal/{deal_id}"
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_folders_by_asset_id(self, asset_id: int, **params):
        url = f"{BASE_URL}/folders/asset/{asset_id}"
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_investments(self, **params):
        response = requests.get(f"{BASE_URL}/investments", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_list_options_by_field_definition_id(self, field_definition_id: str, **params):
        response = requests.get(f"{BASE_URL}/list_options/field_definition/{field_definition_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_loans(self, **params):
        response = requests.get(f"{BASE_URL}/loans", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_people(self, **params):
        response = requests.get(f"{BASE_URL}/people", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_property_by_id(self, property_id: str):
        response = requests.get(f"{BASE_URL}/property/{property_id}", headers=self.headers)
        response.raise_for_status()
        return response.json()

    def get_properties(self, **params):
        response = requests.get(f"{BASE_URL}/properties", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_roles_by_deal_id(self, deal_id: str, **params):
        response = requests.get(f"{BASE_URL}/roles/deal/{deal_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_roles_by_asset_id(self, asset_id: str, **params):
        response = requests.get(f"{BASE_URL}/roles/asset/{asset_id}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def search(self, **params):
        response = requests.get(f"{BASE_URL}/search", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()
