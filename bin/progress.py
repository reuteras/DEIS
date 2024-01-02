#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""."""
#

import os
import subprocess
import sys
import time
from datetime import datetime
from rich.console import Console
from rich.control import Control
from rich import print

max_length = 25

def check_output_contains(command, keyword):
    try:
        result = subprocess.check_output(command, shell=True).decode('utf-8')
        return keyword in result
    except subprocess.CalledProcessError:
        return False

def print_status(text, status):
    colors = {
        'DONE': 'green',
        'RUN SETUP': 'red',
        'RUNNING': 'yellow',
        'NO DOWNLOAD CONTAINER': 'red',
        'NOT RUNNING': 'red',
        'WAITING FOR FILES': 'red',
    }
    # Split the text at the ":" to keep the prefix in default terminal color.
    print(f"{text.ljust(max_length)}: [{colors[status]}]{status.ljust(max_length)}[/{colors[status]}]")


# Create console instance and clear the terminal
console = Console()
console.clear()
console.control(Control.show_cursor(show=False))

try:
    while True:
        console.control(Control.home())

        print("DEIS - progress")
        print("###############")
        print("")
        print(datetime.now().strftime("%c"))
        print("")

        # Checking Setup
        if check_output_contains('docker volume ls', 'deis_elasticsearch'):
            print_status("Setup", "DONE")
        else:
            print_status("Setup", "RUN SETUP")
            sys.exit()

        # Adding URLS to download
        if check_output_contains('docker ps -a', 'deis-downloader') and not os.path.exists('files/added_urls'):
            print_status("Adding URLS to download", "RUNNING")
        elif not check_output_contains('docker ps -a', 'deis-downloader') and not os.path.exists('files/added_urls'):
            print_status("Adding URLS to download", "NO DOWNLOAD CONTAINER")
        else:
            print_status("Adding URLS to download", "DONE")

        # Download 
        if os.path.exists('files/running') and not os.path.exists('files/downloaded'):
            print_status("Download", "RUNNING")
        elif os.path.exists('files/downloaded'):
            print_status("Download", "DONE")
        else:
            print_status("Download", "NOT RUNNING")

        # Move files to ./files
        if os.path.exists('files/extract') and not os.path.exists('files/unpack'):
            print_status("Move files to files", "RUNNING")
        elif os.path.exists('files/unpack'):
            print_status("Move files to files", "DONE")
        else:
            print_status("Move files to files", "WAITING FOR FILES")

        # Extraction of files
        if not os.path.exists('files/unpack') and not os.path.exists('extracted/files/done'):
            print_status("Extraction of files", "WAITING FOR FILES")
        elif os.path.exists('files/unpack') and not os.path.exists('extracted/files/done'):
            print_status("Extraction of files", "RUNNING")
        else:
            print_status("Extraction of files", "DONE")

        # Ingest
        if os.path.exists('extracted/ingest_done'):
            print_status("Ingest", "DONE")
        elif not os.path.exists('extracted/ingest_done') and not os.path.exists('extracted/files/done'):
            print_status("Ingest", "WAITING FOR FILES")
        else:
            print_status("Ingest", "RUNNING")

        print("".ljust(max_length*2))
        print("Information can be delayed up to 60 seconds. Press CTRL-C to exit.")
        print("".ljust(max_length*2))
        print("Download status: http://127.0.0.1:8080/")
        print("Kibana: http://127.0.0.1:5601/")
        time.sleep(0.1)

except KeyboardInterrupt:
    console.show_cursor(True)

