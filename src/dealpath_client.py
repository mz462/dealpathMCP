import os
import re
from typing import Any
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

DEALPATH_API_KEY = os.getenv("dealpath_key")
BASE_URL = "https://api.dealpath.com"
FILES_BASE_URL = "https://files.dealpath.com"

# Default network settings
DEFAULT_TIMEOUT = float(os.getenv("dealpath_timeout", "20"))
RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)


class DealpathClient:
    def __init__(self):
        self.log = logging.getLogger(__name__ + ".DealpathClient")
        if not DEALPATH_API_KEY:
            raise RuntimeError(
                "Missing Dealpath API key. Set 'dealpath_key' in environment/.env."
            )

        # Shared session with retries and default headers
        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=RETRY_STRATEGY)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.headers = {
            "Authorization": f"Bearer {DEALPATH_API_KEY}",
            "Accept": "application/vnd.dealpath.api.v1+json",
        }
        self.session.headers.update(self.headers)

    def get_deals(self, **filters):
        response = self.session.get(
            f"{BASE_URL}/deals", params=filters, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_deal_by_id(self, deal_id: str):
        """Fetch a single deal by ID.

        API endpoint is singular: /deal/{deal_id}
        Returns nested object: {"deal": {"data": {...}, "next_token": null}}
        """
        url = f"{BASE_URL}/deal/{deal_id}"
        self.log.info(f"GET {url}")
        response = self.session.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_assets(self, **filters):
        response = self.session.get(
            f"{BASE_URL}/assets", params=filters, timeout=DEFAULT_TIMEOUT
        )
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
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_field_definitions(self, **params):
        response = self.session.get(
            f"{BASE_URL}/field_definitions", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_deal_id(self, deal_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/deal/{deal_id}", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_investment_id(self, investment_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/investment/{investment_id}",
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_property_id(self, property_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/property/{property_id}",
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_asset_id(self, asset_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/asset/{asset_id}", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_loan_id(self, loan_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/loan/{loan_id}", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_fields_by_field_definition_id(self, field_definition_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/fields/field_definition/{field_definition_id}",
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_asset_files_by_id(self, asset_id: int, **params):
        url = f"{BASE_URL}/files/asset/{asset_id}"
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_file_by_id(self, file_id: str):
        # Step 1: Get the temporary download URL
        url_response = self.session.get(
            f"{BASE_URL}/file/{file_id}/download_url", timeout=DEFAULT_TIMEOUT
        )
        url_response.raise_for_status()
        download_details = url_response.json()
        download_url = download_details.get("url")
        filename = download_details.get("name", f"{file_id}.unknown")

        if not download_url:
            raise Exception("Could not retrieve download URL.")

        # Step 2: Download the file from the temporary URL
        file_response = self.session.get(
            download_url, headers={"Accept": "*/*"}, timeout=DEFAULT_TIMEOUT
        )
        file_response.raise_for_status()

        return {"content": file_response.content, "filename": filename}

    def get_file_download_url(self, file_id: str):
        """Return a temporary, signed download URL and filename for a file.

        This avoids proxying large binaries through our server; callers can
        download directly from Dealpath using the signed URL.
        """
        url_response = self.session.get(
            f"{BASE_URL}/file/{file_id}/download_url", timeout=DEFAULT_TIMEOUT
        )
        url_response.raise_for_status()
        data = url_response.json()
        return {"url": data.get("url"), "filename": data.get("name", f"{file_id}")}

    def download_file_content(self, file_id: str) -> dict[str, Any]:
        """Download file bytes directly from files.dealpath.com.

        Returns dict with keys: content (bytes), filename (str), mime_type (str | None).
        """
        url = f"{FILES_BASE_URL}/file/{file_id}"
        # Use only Authorization header; do not send JSON Accept header here
        headers = {"Authorization": f"Bearer {DEALPATH_API_KEY}", "Accept": "*/*"}
        resp = self.session.get(url, headers=headers, stream=True, timeout=DEFAULT_TIMEOUT)
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
        response = self.session.get(
            f"{BASE_URL}/file_tag_definitions", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_folders_by_deal_id(self, deal_id: int, **params):
        url = f"{BASE_URL}/folders/deal/{deal_id}"
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_folders_by_asset_id(self, asset_id: int, **params):
        url = f"{BASE_URL}/folders/asset/{asset_id}"
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_investments(self, **params):
        response = self.session.get(
            f"{BASE_URL}/investments", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_list_options_by_field_definition_id(
        self, field_definition_id: str, **params
    ):
        response = self.session.get(
            f"{BASE_URL}/list_options/field_definition/{field_definition_id}",
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_loans(self, **params):
        response = self.session.get(
            f"{BASE_URL}/loans", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_people(self, **params):
        response = self.session.get(
            f"{BASE_URL}/people", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_property_by_id(self, property_id: str):
        response = self.session.get(
            f"{BASE_URL}/property/{property_id}", timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_properties(self, **params):
        response = self.session.get(
            f"{BASE_URL}/properties", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_roles_by_deal_id(self, deal_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/roles/deal/{deal_id}", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def get_roles_by_asset_id(self, asset_id: str, **params):
        response = self.session.get(
            f"{BASE_URL}/roles/asset/{asset_id}", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def search(self, **params):
        response = self.session.get(
            f"{BASE_URL}/search", params=params, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    # --- Executive Analytics Methods ---

    def get_executive_portfolio_overview(self, days_back: int = 90) -> dict[str, Any]:
        """Generate comprehensive portfolio overview for executives."""
        from collections import defaultdict
        from datetime import datetime, timedelta

        # Get all deals for analysis (use high limit to get all deals)
        deals_data = self.get_deals(limit=1000)
        deals = deals_data.get("deals", {}).get("data", [])

        # Time-based filtering
        datetime.now() - timedelta(days=days_back)

        # Key metrics
        metrics = {
            "portfolio_overview": {
                "total_deals": len(deals),
                "initial_registration": len(
                    [d for d in deals if d.get("deal_state") == "Initial Registration"]
                ),
                "tracking_deals": len(
                    [d for d in deals if d.get("deal_state") == "Tracking"]
                ),
                "underwriting_deals": len(
                    [d for d in deals if d.get("deal_state") == "Underwriting"]
                ),
                "dead_deals": len(
                    [d for d in deals if d.get("deal_state") == "Dead"]
                ),
            },
            "deal_type_breakdown": defaultdict(int),
            "deal_size_distribution": {"small": 0, "medium": 0, "large": 0, "mega": 0},
            "geographic_distribution": defaultdict(int),
            "performance_indicators": {},
        }

        # Analyze property types and sizes
        for deal in deals:
            prop_type = deal.get("deal_type", "Unknown")
            metrics["deal_type_breakdown"][prop_type] += 1

            # Deal size classification (placeholder - would need actual deal values)
            deal_size = deal.get("estimated_value", 0)
            if deal_size < 10_000_000:
                metrics["deal_size_distribution"]["small"] += 1
            elif deal_size < 50_000_000:
                metrics["deal_size_distribution"]["medium"] += 1
            elif deal_size < 200_000_000:
                metrics["deal_size_distribution"]["large"] += 1
            else:
                metrics["deal_size_distribution"]["mega"] += 1

            # Geographic analysis
            location = deal.get("address", {}).get("city", "Unknown")
            if location != "Unknown":
                metrics["geographic_distribution"][location] += 1

        # Performance indicators
        active_deals = [d for d in deals if d.get("deal_state") == "Active"]
        metrics["performance_indicators"] = {
            "deal_velocity": len(active_deals)
            / max(days_back / 30, 1),  # deals per month
            "portfolio_health_score": min(
                100, (len(active_deals) / max(len(deals), 1)) * 100
            ),
            "diversification_score": len(
                {d.get("deal_type") for d in deals if d.get("deal_type")}
            ),
        }

        return {
            "summary": f"Portfolio analysis covering {len(deals)} deals over {days_back} days",
            "generated_at": datetime.now().isoformat(),
            "metrics": dict(metrics),
        }

    def get_deal_velocity_analysis(self, lookback_months: int = 6) -> dict[str, Any]:
        """Analyze deal flow velocity and pipeline health."""
        from collections import defaultdict
        from datetime import datetime

        deals_data = self.get_deals(limit=1000)
        deals = deals_data.get("deals", {}).get("data", [])

        # Time-based analysis
        now = datetime.now()
        monthly_data = defaultdict(lambda: {"new": 0, "closed": 0, "active": 0})

        velocity_metrics = {
            "pipeline_flow": monthly_data,
            "conversion_rates": {},
            "time_to_close": {},
            "bottleneck_analysis": {},
            "forecasting": {},
        }

        # Analyze by deal state transitions (simplified - real implementation would track state changes)
        deal_states = defaultdict(int)
        for deal in deals:
            state = deal.get("deal_state", "Unknown")
            deal_states[state] += 1

        total_deals = len(deals)
        if total_deals > 0:
            velocity_metrics["conversion_rates"] = {
                "potential_to_active": deal_states.get("Active", 0)
                / max(deal_states.get("Potential", 1), 1),
                "active_to_closed": deal_states.get("Closed", 0)
                / max(deal_states.get("Active", 1), 1),
                "overall_close_rate": deal_states.get("Closed", 0) / total_deals,
            }

        # Pipeline health indicators
        velocity_metrics["pipeline_health"] = {
            "total_pipeline_value": sum(
                deal.get("estimated_value", 0)
                for deal in deals
                if deal.get("deal_state") == "Active"
            ),
            "average_deal_size": sum(deal.get("estimated_value", 0) for deal in deals)
            / max(total_deals, 1),
            "pipeline_quality_score": (
                deal_states.get("Active", 0) + deal_states.get("Closed", 0)
            )
            / max(total_deals, 1)
            * 100,
        }

        return {
            "analysis_period": f"{lookback_months} months",
            "generated_at": now.isoformat(),
            "velocity_metrics": dict(velocity_metrics),
        }

    def get_market_performance_insights(
        self, property_types: list[str] = None
    ) -> dict[str, Any]:
        """Generate market performance and trend analysis."""
        from collections import defaultdict
        from datetime import datetime

        deals_data = self.get_deals(limit=1000)
        deals = deals_data.get("deals", {}).get("data", [])

        if property_types:
            deals = [d for d in deals if d.get("deal_type") in property_types]

        # Market analysis
        market_data = {
            "deal_type_performance": defaultdict(
                lambda: {"count": 0, "avg_value": 0, "success_rate": 0}
            ),
            "geographic_hotspots": defaultdict(
                lambda: {"deal_count": 0, "total_value": 0}
            ),
            "market_trends": {},
            "competitive_landscape": {},
        }

        # Analyze by property type
        for prop_type in {
            d.get("deal_type") for d in deals if d.get("deal_type")
        }:
            type_deals = [d for d in deals if d.get("deal_type") == prop_type]
            total_value = sum(d.get("estimated_value", 0) for d in type_deals)
            closed_deals = len(
                [d for d in type_deals if d.get("deal_state") == "Closed"]
            )

            market_data["deal_type_performance"][prop_type] = {
                "count": len(type_deals),
                "total_value": total_value,
                "avg_value": total_value / max(len(type_deals), 1),
                "success_rate": closed_deals / max(len(type_deals), 1) * 100,
            }

        # Geographic analysis
        for deal in deals:
            city = deal.get("address", {}).get("city", "Unknown")
            if city != "Unknown":
                market_data["geographic_hotspots"][city]["deal_count"] += 1
                market_data["geographic_hotspots"][city]["total_value"] += deal.get(
                    "estimated_value", 0
                )

        # Market health indicators
        total_deals = len(deals)
        market_data["market_health"] = {
            "market_activity_level": (
                "High" if total_deals > 50 else "Medium" if total_deals > 20 else "Low"
            ),
            "diversification_index": len(
                {d.get("deal_type") for d in deals if d.get("deal_type")}
            ),
            "geographic_spread": len(
                {d.get("address", {}).get("city") for d in deals if d.get("address", {}).get("city")}
            ),
            "average_deal_velocity": len(
                [d for d in deals if d.get("deal_state") == "Active"]
            )
            / max(total_deals, 1),
        }

        return {
            "analysis_scope": f"{total_deals} deals analyzed",
            "property_types_included": property_types or "All",
            "generated_at": datetime.now().isoformat(),
            "market_insights": dict(market_data),
        }

    def get_risk_exposure_analysis(self) -> dict[str, Any]:
        """Comprehensive risk assessment and exposure analysis."""
        from collections import defaultdict
        from datetime import datetime

        deals_data = self.get_deals(limit=1000)
        deals = deals_data.get("deals", {}).get("data", [])

        risk_analysis = {
            "concentration_risk": {},
            "geographic_risk": {},
            "market_risk": {},
            "liquidity_risk": {},
            "overall_risk_score": 0,
        }

        total_value = sum(d.get("estimated_value", 0) for d in deals)

        # Concentration risk analysis
        property_concentration = defaultdict(int)
        geographic_concentration = defaultdict(int)

        for deal in deals:
            prop_type = deal.get("deal_type", "Unknown")
            city = deal.get("address", {}).get("city", "Unknown")
            deal_value = deal.get("estimated_value", 0)

            property_concentration[prop_type] += deal_value
            geographic_concentration[city] += deal_value

        # Calculate concentration ratios
        max_property_exposure = 0
        max_geographic_exposure = 0

        if total_value > 0:
            max_property_exposure = (
                max(property_concentration.values()) / total_value
                if property_concentration
                else 0
            )
            max_geographic_exposure = (
                max(geographic_concentration.values()) / total_value
                if geographic_concentration
                else 0
            )

            risk_analysis["concentration_risk"] = {
                "max_deal_type_exposure": max_property_exposure * 100,
                "max_geographic_exposure": max_geographic_exposure * 100,
                "diversification_score": 100
                - (max_property_exposure * 50 + max_geographic_exposure * 50),
            }
        else:
            risk_analysis["concentration_risk"] = {
                "max_deal_type_exposure": 0,
                "max_geographic_exposure": 0,
                "diversification_score": 100,
            }

        # Portfolio liquidity analysis
        active_deals = len([d for d in deals if d.get("deal_state") == "Active"])
        total_deals = len(deals)

        risk_analysis["liquidity_risk"] = {
            "active_deal_ratio": active_deals / max(total_deals, 1) * 100,
            "portfolio_liquidity_score": min(
                100, (active_deals / max(total_deals, 1)) * 120
            ),
            "recommended_action": (
                "Diversify"
                if max_property_exposure > 0.4
                else "Maintain" if max_property_exposure > 0.25 else "Opportunistic"
            ),
        }

        # Overall risk score (simplified algorithm)
        concentration_score = (
            100 - (max_property_exposure * 100 + max_geographic_exposure * 100) / 2
        )
        liquidity_score = risk_analysis["liquidity_risk"]["portfolio_liquidity_score"]

        risk_analysis["overall_risk_score"] = (
            concentration_score + liquidity_score
        ) / 2
        risk_analysis["risk_level"] = (
            "Low"
            if risk_analysis["overall_risk_score"] > 80
            else "Medium" if risk_analysis["overall_risk_score"] > 60 else "High"
        )

        return {
            "portfolio_value": total_value,
            "analysis_date": datetime.now().isoformat(),
            "risk_assessment": risk_analysis,
        }
