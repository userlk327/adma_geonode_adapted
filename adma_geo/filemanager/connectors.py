"""Connectors: outside services a person links to ADMA so Ask ADMA can use them for them.

HCC Swan and Globus work now (their sign-in and runs live in askadma_swan / askadma_studio / askadma_globus).
OneDrive is listed but stays switched off until ADMA has an https address and an app
registration approved by UNL IT; the settings below turn it on later.
"""
import os

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import askadma_swan as W
from . import askadma_globus as G

# Filled in once UNL IT approves an app in Microsoft 365 (Entra ID) for ADMA.
ONEDRIVE_SETTINGS = ("MS_CLIENT_ID", "MS_TENANT_ID", "MS_REDIRECT_URI")


def onedrive_status():
    missing = [k for k in ONEDRIVE_SETTINGS if not os.environ.get(k)]
    needs = []
    if missing:
        needs.append("An app for ADMA in UNL's Microsoft 365, approved by UNL IT.")
    redirect = os.environ.get("MS_REDIRECT_URI", "")
    if not redirect.startswith("https://"):
        needs.append("A secure https web address for ADMA. Microsoft only signs people in to https sites.")
    return {"ready": not needs, "needs": needs}


@login_required
def connectors_view(request):
    return render(request, "filemanager/connectors.html", {
        "page_title": "Connectors",
        "swan_available": W.ssh_available(),
        "swan_host": W.SWAN_HOST,
        "onedrive": onedrive_status(),
        "globus_configured": G.configured(),
        "globus_sides": [("src", "From", "Folder or file to copy, e.g. /~/ADAPT/Test101/"),
                         ("dst", "To", "Destination folder, e.g. /attic/<group>/adapt_weekly/")],
    })
