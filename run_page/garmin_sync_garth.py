"""
Python 3 API wrapper for Garmin Connect to get your statistics.
Copy most code from https://github.com/cyberjunky/python-garminconnect
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback
import zipfile
from io import BytesIO

import aiofiles
import cloudscraper
import garth
import httpx
from config import FOLDER_DICT, JSON_FILE, SQL_FILE, config
from tenacity import retry, stop_after_attempt, wait_fixed

from garmin_device_adaptor import wrap_device_info
from utils import make_activities_file_only

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

TIME_OUT = httpx.Timeout(240.0, connect=360.0)
GARMIN_COM_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.com/sso",
    "MODERN_URL": "https://connectapi.garmin.com",
    "SIGNIN_URL": "https://sso.garmin.com/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.com/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.com/activity-service/activity/{activity_id}",
}

GARMIN_CN_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.cn/sso",
    "MODERN_URL": "https://connectapi.garmin.cn",
    "SIGNIN_URL": "https://sso.garmin.cn/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.cn/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.cn/activity-service/activity/{activity_id}",
}

# set to True if you want to sync all time activities
# default only sync last 20
GET_ALL = False


class Garmin:
    def __init__(self, email, password, auth_domain, is_only_running=False):
        """
        Init module
        """
        self.email = email
        self.password = password
        self.req = httpx.AsyncClient(timeout=TIME_OUT)
        self.cf_req = cloudscraper.CloudScraper()
        self.URL_DICT = (
            GARMIN_CN_URL_DICT
            if auth_domain and str(auth_domain).upper() == "CN"
            else GARMIN_COM_URL_DICT
        )
        self.modern_url = self.URL_DICT.get("MODERN_URL")

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.88 Safari/537.36",
            "origin": self.URL_DICT.get("SSO_URL_ORIGIN"),
            "nk": "NT",
        }
        self.is_only_running = is_only_running
        self.upload_url = self.URL_DICT.get("UPLOAD_URL")
        self.activity_url = self.URL_DICT.get("ACTIVITY_URL")
        self.is_login = False

        self.garth = garth.Client(
            domain="garmin.cn"
            if auth_domain and str(auth_domain).upper() == "CN"
            else "garmin.com"
        )
        self.token = None

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(30))
    def login(self):
        """
        Login to portal
        """
        if self.token is not None:
            # Try to get this token
            try:
                self.garth.load(self.token)
                self.is_login = True
            except Exception as err:
                self.is_login = False
                raise GarminConnectConnectionError("Error connecting") from err

        if self.is_login is False:
            try:
                self.garth.login(options.email, options.password)
                self.token = garth.client.dumps()
                self.is_login = True
            except Exception as err:
                self.is_login = False

    async def get_activities(self, start, limit):
        """
        Fetch available activities
        """
        if not self.is_login:
            self.login()
        url = "/activitylist-service/activities/search/activities"
        params = {"start": str(start), "limit": str(limit)}

        if self.is_only_running:
            params.update({"activityType": "running"})
        return self.garth.connectapi(url, params=params)

    async def download_activity(self, activity_id, file_type="gpx"):
        activity_id = str(activity_id)

        url = None
        if file_type in ["gpx", "tcx", "kml", "csv"]:
            url = f"/download-service/export/{file_type}/activity/{activity_id}"
        elif file_type == "fit":
            url = f"/download-service/files/activity/{activity_id}"

        logger.info(f"Download activity from {url}")

        return self.garth.download(url)

    async def upload_activities(self, files):
        if not self.is_login:
            self.login()
        for file, garmin_type in files:
            file_base_name = os.path.basename(file)
            file_extension = file_base_name.split(".")[-1]
            allowed_file_extension = file_extension.lower() in [
                "gpx",
                "tcx",
                "kml",
                "csv",
                "fit",
            ]

            if allowed_file_extension:
                filedata = {
                    "file": (file_base_name, open(file, "rb" or "r")),
                }
                url = "/upload-service/upload"
                return self.garth.post("connectapi", url, files=filedata, api=True)
            else:
                raise GarminConnectInvalidFileFormatError(f"Could not upload {file}")

    async def upload_activities_original_from_strava(
        self, datas, use_fake_garmin_device=False
    ):
        print(
            "start upload activities to garmin!, use_fake_garmin_device:",
            use_fake_garmin_device,
        )
        if not self.is_login:
            self.login()
        for data in datas:
            print(data.filename)
            with open(data.filename, "wb") as f:
                for chunk in data.content:
                    f.write(chunk)
            f = open(data.filename, "rb")
            # wrap fake garmin device to origin fit file, current not support gpx file
            if use_fake_garmin_device:
                file_body = wrap_device_info(f)
            else:
                file_body = BytesIO(f.read())
            files = {"file": (data.filename, file_body)}

            try:
                res = await self.req.post(
                    self.upload_url, files=files, headers=self.headers
                )
                os.remove(data.filename)
                f.close()
            except Exception as e:
                print(str(e))
                # just pass for now
                continue
            try:
                resp = res.json()["detailedImportResult"]
                print("garmin upload success: ", resp)
            except Exception as e:
                print("garmin upload failed: ", e)
        await self.req.aclose()


class GarminConnectHttpError(Exception):
    def __init__(self, status):
        super(GarminConnectHttpError, self).__init__(status)
        self.status = status


class GarminConnectConnectionError(Exception):
    """Raised when communication ended in error."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectConnectionError, self).__init__(status)
        self.status = status


class GarminConnectTooManyRequestsError(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectTooManyRequestsError, self).__init__(status)
        self.status = status


class GarminConnectAuthenticationError(Exception):
    """Raised when login returns wrong result."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectAuthenticationError, self).__init__(status)
        self.status = status


class GarminConnectInvalidFileFormatError(Exception):
    """Raised when an invalid file format is passed to upload."""


async def download_garmin_data(client, activity_id, file_type="gpx"):
    folder = FOLDER_DICT.get(file_type, "gpx")
    try:
        file_data = await client.download_activity(activity_id, file_type=file_type)
        file_path = os.path.join(folder, f"{activity_id}.{file_type}")
        need_unzip = False
        if file_type == "fit":
            file_path = os.path.join(folder, f"{activity_id}.zip")
            need_unzip = True
        async with aiofiles.open(file_path, "wb") as fb:
            await fb.write(file_data)
        if need_unzip:
            zip_file = zipfile.ZipFile(file_path, "r")
            for file_info in zip_file.infolist():
                zip_file.extract(file_info, folder)
                os.rename(
                    os.path.join(folder, f"{activity_id}_ACTIVITY.fit"),
                    os.path.join(folder, f"{activity_id}.fit"),
                )
            os.remove(file_path)
    except:
        print(f"Failed to download activity {activity_id}: ")
        traceback.print_exc()


async def get_activity_id_list(client, start=0):
    if GET_ALL:
        activities = await client.get_activities(start, 100)
        if len(activities) > 0:
            ids = list(map(lambda a: str(a.get("activityId", "")), activities))
            print(f"Syncing Activity IDs")
            return ids + await get_activity_id_list(client, start + 100)
        else:
            return []
    else:
        activities = await client.get_activities(start, 40)
        if len(activities) > 0:
            ids = list(map(lambda a: str(a.get("activityId", "")), activities))
            print(f"Syncing Activity IDs")
            return ids
        else:
            return []


async def gather_with_concurrency(n, tasks):
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task

    return await asyncio.gather(*(sem_task(task) for task in tasks))


def get_downloaded_ids(folder):
    return [i.split(".")[0] for i in os.listdir(folder) if not i.startswith(".")]


async def download_new_activities(
    email, password, auth_domain, downloaded_ids, is_only_running, folder, file_type
):
    client = Garmin(email, password, auth_domain, is_only_running)
    client.login()
    # because I don't find a para for after time, so I use garmin-id as filename
    # to find new run to generage
    activity_ids = await get_activity_id_list(client)
    to_generate_garmin_ids = list(set(activity_ids) - set(downloaded_ids))
    print(f"{len(to_generate_garmin_ids)} new activities to be downloaded")

    start_time = time.time()
    await gather_with_concurrency(
        10,
        [
            download_garmin_data(client, id, file_type=file_type)
            for id in to_generate_garmin_ids
        ],
    )
    print(f"Download finished. Elapsed {time.time()-start_time} seconds")

    await client.req.aclose()
    return to_generate_garmin_ids


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("email", nargs="?", help="email of garmin")
    parser.add_argument("password", nargs="?", help="password of garmin")
    parser.add_argument(
        "--is-cn",
        dest="is_cn",
        action="store_true",
        help="if garmin accout is cn",
    )
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="if is only for running",
    )
    parser.add_argument(
        "--tcx",
        dest="download_file_type",
        action="store_const",
        const="tcx",
        default="gpx",
        help="to download personal documents or ebook",
    )
    parser.add_argument(
        "--fit",
        dest="download_file_type",
        action="store_const",
        const="fit",
        default="gpx",
        help="to download personal documents or ebook",
    )
    options = parser.parse_args()
    email = options.email or config("sync", "garmin", "email")
    password = options.password or config("sync", "garmin", "password")
    auth_domain = (
        "CN" if options.is_cn else config("sync", "garmin", "authentication_domain")
    )
    file_type = options.download_file_type
    is_only_running = options.only_run
    if email == None or password == None:
        print("Missing argument nor valid configuration file")
        sys.exit(1)
    folder = FOLDER_DICT.get(file_type, "gpx")
    # make gpx or tcx dir
    if not os.path.exists(folder):
        os.mkdir(folder)
    downloaded_ids = get_downloaded_ids(folder)

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(
        download_new_activities(
            email,
            password,
            auth_domain,
            downloaded_ids,
            is_only_running,
            folder,
            file_type,
        )
    )
    loop.run_until_complete(future)
    make_activities_file_only(SQL_FILE, folder, JSON_FILE, file_suffix=file_type)
