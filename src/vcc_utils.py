import re
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from database import Session

from vcc.client import VCC
from vcc.utils import json_decoder
import vcc.settings


def get_file(vcc_config: str, file_path: str) -> requests.Response:
    # Init VCC settings
    vcc.settings.init(path=vcc_config)
    # Retrieve file from VCC
    with VCC() as client:
        return client.get(file_path)


def get_upcoming_sessions(vcc_config: str, days: int=1) -> List:
    # Get upcoming session for station
    vcc.settings.init(path=vcc_config)
    code = vcc.settings.Signatures.NS[0]  # Extract station code from vcc config file
    params = {'days': days + 1, 'begin': datetime.now(tz=timezone.utc) - timedelta(days=1)}
    with VCC() as client:
        if not (codes := json_decoder(client.get(f'/sessions/next/{code}', params=params).json())):
            return []
        return [json_decoder(rsp.json()) for code in codes if (rsp := client.get(f"/sessions/{code}"))]

