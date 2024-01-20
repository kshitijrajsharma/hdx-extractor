import argparse
import copy
import json
import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HDXProcessor:
    def __init__(
        self,
        config_json=None,
    ):
        if config_json is None:
            raise ValueError("Config JSON couldn't be found")

        if isinstance(config_json, dict):
            self.config = config_json
        elif os.path.exists(config_json):
            with open(config_json) as f:
                self.config = json.load(f)
        else:
            raise ValueError("Invalid value for config_json")

        self.RAW_DATA_API_BASE_URL = os.environ.get(
            "RAW_DATA_API_BASE_URL", "https://api-prod.raw-data.hotosm.org/v1"
        )
        self.RAW_DATA_SNAPSHOT_URL = f"{self.RAW_DATA_API_BASE_URL}/custom/snapshot/"
        self.RAWDATA_API_AUTH_TOKEN = os.environ.get("RAWDATA_API_AUTH_TOKEN")

    def generate_filtered_config(self, export):
        config_temp = copy.deepcopy(self.config)
        for key in export["properties"].keys():
            config_temp["key"] = export["properties"].get(key)
        return json.dumps(config_temp)

    def process_export(self, export):
        request_config = self.generate_filtered_config(export)
        response = self.retry_post_request(request_config)
        return response

    def retry_post_request(self, request_config):
        retry_strategy = Retry(
            total=2,  # Number of retries
            status_forcelist=[429, 502],
            allowed_methods=["POST"],
            backoff_factor=1,
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        with requests.Session() as req_session:
            req_session.mount("https://", adapter)
            req_session.mount("http://", adapter)

        try:
            HEADERS = {
                "Content-Type": "application/json",
                "Access-Token": self.RAWDATA_API_AUTH_TOKEN,
            }
            response = req_session.post(
                self.RAW_DATA_SNAPSHOT_URL,
                headers=HEADERS,
                data=request_config,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()["task_id"]
        except requests.exceptions.RetryError as e:
            self.handle_rate_limit()
            return self.retry_post_request(request_config)

    def handle_rate_limit(self):
        logging.warning("Rate limit reached. Waiting for 1 minute before retrying.")
        time.sleep(61)

    def retry_get_request(self, url):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error("Error in GET request: %s", str(e))
            return {"status": "ERROR"}

    def track_tasks_status(self, task_ids):
        results = {}

        for task_id in task_ids:
            status_url = f"{self.RAW_DATA_API_BASE_URL}/tasks/status/{task_id}/"
            response = self.retry_get_request(status_url)

            if response["status"] == "SUCCESS":
                results[task_id] = response["result"]
            elif response["status"] in ["PENDING", "STARTED"]:
                while True:
                    response = self.retry_get_request(status_url)
                    if response["status"] in ["SUCCESS", "ERROR", "FAILURE"]:
                        results[task_id] = response["result"]
                        logging.info(
                            "Task %s is %s , Moving to fetch next one",
                            task_id,
                            response["status"],
                        )
                        break
                    logging.warning(
                        "Task %s is %s. Retrying in 30 seconds...",
                        task_id,
                        response["status"],
                    )
                    time.sleep(30)
            else:
                results[task_id] = "FAILURE"
        logging.info("%s tasks stats is fetched, Dumping result", len(results))
        with open("result.json", "w") as f:
            json.dump(results, f, indent=2)
        logging.info("Done ! Find result at result.json")

    def get_scheduled_exports(self, frequency):
        max_retries = 3
        for retry in range(max_retries):
            try:
                active_projects_api_url = f"{self.RAW_DATA_API_BASE_URL}/hdx/queries/scheduled/?interval={frequency}"
                response = requests.get(active_projects_api_url, timeout=10)
                response.raise_for_status()
                return response.json()["features"]
            except Exception as e:
                logging.warn(
                    f" : Request failed (attempt {retry + 1}/{max_retries}): {e}"
                )
        raise Exception(f"Failed to fetch scheduled projects {max_retries} attempts")

    def init_call(self, countries=None, fetch_scheduled_exports=None):
        all_export_details = []
        if countries:
            for country in countries:
                all_export_details.append({"properties": {"iso3": country}})
        if fetch_scheduled_exports:
            frequency = fetch_scheduled_exports
            logger.info(
                "Retrieving scheduled projects with frequency of  %s",
                frequency,
            )

            all_export_details.extend(self.get_scheduled_exports(frequency))

        task_ids = []

        logger.info("Supplied %s exports", len(all_export_details))
        for export in all_export_details:
            task_id = self.process_export(export)
            if task_id is not None:
                task_ids.append(task_id)
        logging.info(
            "Request : All request to Raw Data API has been sent, Logging %s task_ids",
            len(task_ids),
        )
        logging.info(task_ids)
        return task_ids


def lambda_handler(event, context):
    config_json = os.environ.get("CONFIG_JSON", None)
    if config_json is None:
        raise ValueError("Config JSON couldn't be found in env")
    if os.environ.get("RAWDATA_API_AUTH_TOKEN", None) is None:
        raise ValueError("RAWDATA_API_AUTH_TOKEN environment variable not found.")
    countries = event.get("countries", None)
    fetch_scheduled_exports = event.get("fetch_scheduled_exports", "daily")

    hdx_processor = HDXProcessor(config_json)
    hdx_processor.init_call(
        countries=countries, fetch_scheduled_exports=fetch_scheduled_exports
    )


def main():
    parser = argparse.ArgumentParser(
        description="Triggers extraction request for Hdx extractions projects"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--countries",
        nargs="+",
        type=str,
        help="List of country ISO3 codes, add multiples by space",
    )
    group.add_argument(
        "--fetch-scheduled-exports",
        nargs="?",
        const="daily",
        type=str,
        metavar="frequency",
        help="Fetch schedule exports with an optional frequency (default is daily)",
    )
    parser.add_argument(
        "--track",
        action="store_true",
        default=False,
        help="Track the status of tasks and dumps result, Use it carefully as it waits for all tasks to complete",
    )
    args = parser.parse_args()

    config_json = os.environ.get("CONFIG_JSON", "config.json")
    if os.environ.get("RAWDATA_API_AUTH_TOKEN", None) is None:
        raise ValueError("RAWDATA_API_AUTH_TOKEN environment variable not found.")

    hdx_processor = HDXProcessor(config_json)
    task_ids = hdx_processor.init_call(
        countries=args.countries, fetch_scheduled_exports=args.fetch_scheduled_exports
    )
    if args.track:
        hdx_processor.track_tasks_status(task_ids)


if __name__ == "__main__":
    main()
