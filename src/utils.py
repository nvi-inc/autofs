import os
from datetime import datetime, date
import toml
import re
import traceback

from pathlib import Path

from subprocess import Popen, PIPE, DEVNULL
from typing import Optional, List, Any


def exec_app(command: List[Any], wait:bool=False, cwd:str=None, nohup:bool=False) -> Optional[str]:
    """
    Execute command using Popen
    :param command: Command and options input as list
    :param wait: Wait for response
    :param cwd: Change working directory
    :param nohup: Run app in background
    :return: Response on None
    """
    try:
        env = {'PATH': ':'.join(["/usr2/st/bin", "/usr2/fs/bin", os.environ.get('PATH')])}
        if wait:
            return Popen(command, env=env, stdout=PIPE, cwd=cwd).communicate()[0].decode('utf-8').strip()

        if nohup:
            Popen(['nohup'] + command, env=env, stdout=DEVNULL, stderr=DEVNULL, cwd=cwd, preexec_fn=os.setpgrp)
        else:
            Popen(command, env=env, stdout=PIPE, cwd=cwd)
        return "Ok"

    except FileNotFoundError as exc:
        return f"{command} failed {str(exc)}"
    except Exception as exc:
        return f"{command} failed {str(exc)}"


def encoder(obj):
    """
    Encode date and datetime object in iso format string
    :param obj: Any data type
    :return: Encoded or original data
    """
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {name: encoder(item) for name, item in obj.items()}
    if isinstance(obj, list):
        return [encoder(item) for item in obj]
    return obj


def decoder(obj):
    """
    Change any iso format string to datetime or date.
    :param obj: Any data type.
    :return: decoded or original data.
    """
    if isinstance(obj, dict):
        return {k: decoder(val) for k, val in obj.items()}
    if isinstance(obj, list):
        return [decoder(val) for val in obj]
    try:
        return datetime.fromisoformat(obj)
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(obj)
        except (ValueError, TypeError):
            return obj


# Transform data to object attributes
def set_attrs(obj, data):
    # Set attribute of the class
    for key, value in data.items():
        # Check if data already exists
        if item := getattr(obj, key, None):
            set_attrs(item, value) if isinstance(value, dict) else setattr(obj, key, value)
        elif isinstance(value, dict):
            setattr(obj, key, set_attrs(type('', (), {}), value))
        else:
            setattr(obj, key, value)
    return obj


class Config:
    """
    Configuration class populated with toml file.
    Since the config file can be edited, it is not certain that some coded values will be kept.
    This class is used to return None when a variable is not in config to avoid crashes.
    """
    def __init__(self, path):
        self.path = Path(path)
        with open(self.path) as f:
            set_attrs(self, decoder(toml.load(f)))

    def __getattr__(self, item):
        def make_dict(names):
            if not names:
                return None
            return {names[0]: make_dict(names[1:])}

        stack = traceback.extract_stack(limit=2)
        if (dot_items := traceback.format_list(stack)[0].split('\n')[1].strip().partition('.'))[1]:
            items = [re.findall(r'\w+', item)[0] for item in dot_items[2].split('.')]
            set_attrs(self, make_dict(items))
            return getattr(self, item)
