"""
askadma — an in-page AI agent for ADMA.
Backend-switchable: NRP (OpenAI-compatible) or Anthropic, set by AGENT_BACKEND.
Tools are backed by ADMA's database/models: navigate, search, read, open.
"""
import os
import json
import csv
import threading
import time

from django.http import JsonResponse, StreamingHttpResponse, FileResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from .models import Folder, File

BACKEND = os.environ.get("AGENT_BACKEND", "nrp")
NRP_MODEL = os.environ.get("AGENT_MODEL", "glm-5")  # any NRP model id, e.g. deepseek-v4-flash
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_STEPS = 8
SYSTEM_PROMPT = (
    "You are Ask ADMA, the assistant built into ADMA, a research data platform. "
    "Use the tools to answer; never guess file contents, counts or numbers. "
    "Keep answers short: usually one to four sentences or a short list. Use a table only when comparing "
    "several items, and no headers or emojis. Report tool results honestly, including low confidence or "
    "nothing found. Offer at most one follow-up. You only act when the user sends a message, so never "
    "promise to do something later on your own. ADAPT data is read-only."
)


# ---------- Tool functions (unchanged logic) ----------

def tool_list_folders():
    folders = Folder.objects.filter(parent=None).order_by("name")
    return json.dumps([
        {"name": f.name, "is_third_party": f.is_third_party,
         "subfolders": f.subfolders.count(), "files": f.files.count()}
        for f in folders
    ])


def tool_list_folder_contents(folder_name):
    folder = Folder.objects.filter(name__iexact=folder_name).first()
    if not folder:
        return json.dumps({"error": f"No folder named '{folder_name}' found."})
    return json.dumps({
        "folder": folder.name,
        "subfolders": [sf.name for sf in folder.subfolders.all().order_by("name")],
        "files": [{"name": fi.name, "type": fi.file_type, "size": fi.file_size}
                  for fi in folder.files.all().order_by("name")],
    })


def tool_read_file_info(file_name):
    files = File.objects.filter(name__iexact=file_name)[:5]
    if not files:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    return json.dumps([
        {"name": fi.name, "type": fi.file_type, "size": fi.file_size,
         "location": fi.folder.get_full_path() if fi.folder else "(root)"}
        for fi in files
    ])


def tool_read_file_data(file_name, max_rows=50):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    if fi.file_type not in ("csv", "spreadsheet"):
        return json.dumps({"error": f"File '{file_name}' is type '{fi.file_type}', not CSV/spreadsheet."})
    try:
        if fi.file_type == "csv":
            with fi.open_content("r") as fh:
                reader = csv.reader(fh)
                rows = [row for i, row in enumerate(reader) if i <= max_rows]
            return json.dumps({"columns": rows[0] if rows else [], "rows": rows[1:],
                               "note": f"Up to {max_rows} rows."})
        else:
            import pandas as pd
            with fi.open_content("rb") as fh:
                df = pd.read_excel(fh).head(max_rows)
            return json.dumps({"columns": list(df.columns.astype(str)),
                               "rows": df.astype(str).values.tolist(),
                               "note": f"Up to {max_rows} rows."})
    except Exception as e:
        return json.dumps({"error": f"Could not read '{file_name}': {e}"})


def tool_search(query):
    folders = Folder.objects.filter(name__icontains=query)[:20]
    files = File.objects.filter(name__icontains=query)[:30]
    results = {
        "folders": [{"name": f.name, "path": f.get_full_path()} for f in folders],
        "files": [{"name": fi.name, "type": fi.file_type,
                   "location": fi.folder.get_full_path() if fi.folder else "(root)"} for fi in files],
    }
    if not results["folders"] and not results["files"]:
        return json.dumps({"message": f"Nothing found matching '{query}'."})
    return json.dumps(results)


def tool_open_file(file_name):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/file/{fi.id}/"},
                       "message": f"Opening {fi.name}."})


def tool_open_folder(folder_name):
    folder = Folder.objects.filter(name__iexact=folder_name).first()
    if not folder:
        return json.dumps({"error": f"No folder named '{folder_name}' found."})
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/folder/{folder.id}/"},
                       "message": f"Opening the {folder.name} folder."})




def tool_summarize_file(file_name):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type in ("csv", "spreadsheet"):
        return tool_read_file_data(file_name, max_rows=30)
    if fi.file_type == "text":
        try:
            with fi.open_content("r") as fh:
                text = fh.read(4000)
            return json.dumps({"name": fi.name, "text_preview": text})
        except Exception as e:
            return json.dumps({"error": "Could not read file."})
    return json.dumps({"name": fi.name, "type": fi.file_type, "note": "No preview for this type."})


def tool_get_context(current_url=""):
    import re
    if not current_url:
        return json.dumps({"note": "No current page context."})
    m = re.search(r"/folder/([0-9a-f-]+)/", current_url)
    if m:
        folder = Folder.objects.filter(id=m.group(1)).first()
        if folder:
            return json.dumps({"viewing": "folder", "name": folder.name, "path": folder.get_full_path()})
    m = re.search(r"/file/([0-9a-f-]+)/", current_url)
    if m:
        fi = File.objects.filter(id=m.group(1)).first()
        if fi:
            return json.dumps({"viewing": "file", "name": fi.name, "type": fi.file_type})
    return json.dumps({"viewing": "page", "url": current_url})




def tool_read_image(file_name, question="Describe this image in detail."):
    import base64, io
    from PIL import Image
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type != "image":
        return json.dumps({"error": "That file is not an image."})
    try:
        with fi.open_content("rb") as fh:
            raw = fh.read()
        # Resize down so the vision model gets a clean, bounded image
        img = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        from openai import OpenAI
        client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"])
        resp = client.chat.completions.create(
            model="qwen3-vl",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }],
            max_tokens=700,
        )
        desc = resp.choices[0].message.content
        if not desc or not desc.strip():
            return json.dumps({"image": fi.name, "error": "The vision model returned an empty description."})
        return json.dumps({"image": fi.name, "description": desc})
    except Exception as e:
        return json.dumps({"error": "Could not analyze image: " + str(e)})


def tool_plot_data(file_name, x_column, y_column, chart_type="bar"):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type not in ("csv", "spreadsheet"):
        return json.dumps({"error": "That file is not a CSV or spreadsheet."})
    try:
        if fi.file_type == "csv":
            with fi.open_content("r") as fh:
                reader = csv.reader(fh)
                rows = [row for row in reader]
            headers = rows[0]
            data_rows = rows[1:200]
        else:
            import pandas as pd
            with fi.open_content("rb") as fh:
                df = pd.read_excel(fh).head(200)
            headers = list(df.columns.astype(str))
            data_rows = df.astype(str).values.tolist()
        if x_column not in headers or y_column not in headers:
            return json.dumps({"error": "Column not found. Available: " + ", ".join(headers)})
        xi = headers.index(x_column)
        yi = headers.index(y_column)
        labels = [r[xi] for r in data_rows]
        values = []
        for r in data_rows:
            try:
                values.append(float(r[yi]))
            except Exception:
                values.append(0)
        return json.dumps({
            "ui_action": {
                "type": "render_chart",
                "chart_type": chart_type,
                "labels": labels,
                "values": values,
                "x_label": x_column,
                "y_label": y_column,
                "title": y_column + " by " + x_column,
            },
            "message": "Here is the " + chart_type + " chart of " + y_column + " by " + x_column + "."
        })
    except Exception as e:
        return json.dumps({"error": "Could not create chart."})


TOOL_FUNCS = {
    "list_folders": lambda a: tool_list_folders(),
    "search": lambda a: tool_search(a.get("query", "")),
    "open_file": lambda a: tool_open_file(a.get("file_name", "")),
    "open_folder": lambda a: tool_open_folder(a.get("folder_name", "")),
    "list_folder_contents": lambda a: tool_list_folder_contents(a.get("folder_name", "")),
    "read_file_info": lambda a: tool_read_file_info(a.get("file_name", "")),
    "read_file_data": lambda a: tool_read_file_data(a.get("file_name", "")),
    "summarize_file": lambda a: tool_summarize_file(a.get("file_name", "")),
    "get_context": lambda a: tool_get_context(a.get("current_url", "")),
    "read_image": lambda a: tool_read_image(a.get("file_name", ""), a.get("question", "Describe this image in detail.")),
    "plot_data": lambda a: tool_plot_data(a.get("file_name", ""), a.get("x_column", ""), a.get("y_column", ""), a.get("chart_type", "bar")),
}


# ---------- Tool schemas (shared properties) ----------

_TOOL_DEFS = [
    ("list_folders", "List the top-level folders in ADMA.", {}, []),
    ("search", "Search for files and folders by name across ADMA. Use when the user wants to find something and you don't know where it is.",
     {"query": {"type": "string", "description": "Text to search for."}}, ["query"]),
    ("open_file", "Open a file's page in the ADMA interface for the user. Use when the user asks to open/view/go to a file.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("open_folder", "Open a folder's page in the ADMA interface. Use when the user asks to open/go to a folder.",
     {"folder_name": {"type": "string"}}, ["folder_name"]),
    ("list_folder_contents", "List the subfolders and files inside a folder (by name). Use to navigate into folders.",
     {"folder_name": {"type": "string"}}, ["folder_name"]),
    ("read_file_info", "Find a file by name; return its type, size, and folder location.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("read_file_data", "Read a CSV/spreadsheet file's columns and first rows, to analyze or summarize.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("summarize_file", 'Read a file (CSV, spreadsheet, or text) and get its contents so you can summarize it.',
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("get_context", "Find out what page, folder, or file the user is currently viewing. Use for requests like 'summarize this' or 'what is in here'.",
     {"current_url": {"type": "string"}}, []),
    ("read_image", 'Look at an image file and describe what is in it. Use when the user asks what is in a photo or to describe/analyze an image. Not reliable for counting or for checking whether a person or animal is present; use detect_objects for that.',
     {"file_name": {"type": "string"}, "question": {"type": "string"}}, ["file_name"]),
    ("plot_data", 'Create a chart from a CSV or spreadsheet file. Specify x_column, y_column (exact column names), and chart_type (bar, line, or pie). The chart is shown to the user.',
     {"file_name": {"type": "string"}, "x_column": {"type": "string"}, "y_column": {"type": "string"}, "chart_type": {"type": "string"}}, ["file_name", "x_column", "y_column"]),
]


# ---------- Upgraded and extra tools (filemanager/askadma_tools.py) ----------
# Tools there replace same-named ones above (search, list_folder_contents, open_folder, open_file,
# read_image, plot_data) and add image_metadata, detect_objects, scan_folder, scan_status, analyze_data.
from . import askadma_tools as _extra

TOOL_FUNCS.update(_extra.TOOL_FUNCS)
_TOOL_DEFS = [d for d in _TOOL_DEFS if d[0] not in _extra.TOOL_FUNCS] + list(_extra.TOOL_DEFS)


def openai_tools():
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req},
        },
    } for (name, desc, props, req) in _TOOL_DEFS]


def _run_tool(name, args):
    """Run one tool. Returns (text for the model, screen actions, suggested buttons).
    Screen actions and buttons go to the chat panel only; the model just sees what was shown."""
    func = TOOL_FUNCS.get(name)
    if not func:
        return json.dumps({"error": f"Unknown tool '{name}'."}), [], []
    try:
        result = func(args)
    except Exception as e:
        return json.dumps({"error": f"The {name} tool failed: {e}"}), [], []
    actions, buttons = [], []
    try:
        parsed = json.loads(result)
    except Exception:
        return result, actions, buttons
    if isinstance(parsed, dict):
        if parsed.get("ui_action"):
            actions.append(parsed.pop("ui_action"))
        actions.extend(parsed.pop("ui_actions", None) or [])
        buttons = parsed.pop("suggested_actions", None) or []
        if actions:
            parsed["shown_to_user"] = [a.get("type") for a in actions]
        result = json.dumps(parsed, default=str)
    return result, actions, buttons


_STEP_LABELS = {
    "search": "Searching files", "list_folders": "Listing folders", "list_folder_contents": "Opening the folder list",
    "open_file": "Opening the file", "open_folder": "Opening the folder", "show_in_folder": "Finding it in its folder",
    "show_photos": "Collecting photos", "read_image": "Looking at the photo", "image_metadata": "Reading photo details",
    "detect_objects": "Detecting objects", "scan_folder": "Scanning photos", "scan_status": "Checking the scan",
    "analyze_data": "Analyzing the data", "plot_data": "Drawing the chart", "read_file_info": "Finding the file",
    "open_page": "Opening the page", "run_script": "Preparing the script run", "list_runs": "Looking up your runs",
    "script_run_status": "Checking the script run", "save_report": "Saving to ADMA Studio",
    "open_studio": "Opening your ADMA Studio",
    "read_file_data": "Reading the data", "summarize_file": "Reading the file", "get_context": "Checking this page",
}


def _step_label(name, args):
    label = _STEP_LABELS.get(name, "Working")
    objects = args.get("objects")
    objects = ", ".join(map(str, objects)) if isinstance(objects, list) else (objects or "")
    what = str(args.get("file_name") or args.get("script") or args.get("query") or args.get("object") or objects
               or args.get("page") or args.get("title") or "")
    where = str(args.get("folder") or args.get("folder_name") or "")
    target = f"{what} in {where}" if what and where else (what or where)
    return f"{label}: {target}" if target else label


def _clean_history(history):
    """Only user/assistant text from the browser, last 12 messages (never system or tool messages)."""
    out = []
    for m in history or []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip():
            out.append({"role": m["role"], "content": str(m["content"])[:8000]})
    return out[-12:]


def _unique_buttons(buttons, limit=6):
    seen, out = set(), []
    for b in buttons:
        key = (b.get("label"), json.dumps(b.get("action"), sort_keys=True, default=str), b.get("prompt"))
        if key not in seen and b.get("label"):
            seen.add(key)
            out.append(b)
    return out[-limit:]


# ---------- The agent loop (OpenAI / NRP) ----------

def _set_tool_user(user):
    """Tools that write (ADMA Studio) need to know who is asking."""
    try:
        from . import askadma_studio
        askadma_studio.set_current_user(user)
    except Exception:
        pass


def _studio_hint():
    try:
        from . import askadma_studio
        return askadma_studio.STUDIO_HINT
    except Exception:
        return ""


AGENT_STEPS = 10     # model turns per answer (tool rounds + the written answer)
STEP_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "4000"))   # reasoning models think inside this budget
RUN_GUIDE = (" When the user asks to run a script, call run_script as soon as you know the script and its inputs "
             "(usually one search is enough). Do not look into earlier runs first, and do not ask about setup, GPU or "
             "time limits unless the user brings them up: the Run card shows everything and lets the user change it "
             "before anything starts. Earlier failures may already be fixed. After a card is shown, say so in one short "
             "sentence and do not repeat what the card shows.")


def run_agent_events(history, current_url="", user=None):
    """Run the agent, yielding progress events for the chat panel:
    {"type": "step", "text": ...} while working, then {"type": "done", "answer", "actions", "suggestions"}."""
    from openai import OpenAI
    client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"])
    system = SYSTEM_PROMPT + _studio_hint() + RUN_GUIDE
    if current_url:
        system += (" The user is currently viewing this page URL: " + current_url +
                   ". If they say 'this', 'here', or 'current', use get_context with this URL.")
    messages = [{"role": "system", "content": system}] + _clean_history(history)
    tools = openai_tools()
    actions, buttons = [], []

    calls_seen = {}

    for step in range(AGENT_STEPS):
        last = step == AGENT_STEPS - 1
        yield {"type": "step", "text": "Thinking" if step == 0 else
               ("Writing the answer" if last else "Thinking about the results")}
        msg = _ask_model(client, messages, tools, final=last)

        if msg.tool_calls and not last:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                key = tc.function.name + json.dumps(args, sort_keys=True, default=str)
                calls_seen[key] = calls_seen.get(key, 0) + 1
                if calls_seen[key] > 1:
                    # e.g. checking the same scan again and again: stop the loop, answer with what we have
                    result = json.dumps({"note": "You already made this exact call in this answer. Do not repeat "
                                                 "it; answer the user now with what you have."})
                else:
                    yield {"type": "step", "text": _step_label(tc.function.name, args)}
                    _set_tool_user(user)
                    result, new_actions, new_buttons = _run_tool(tc.function.name, args)
                    actions.extend(new_actions)
                    buttons.extend(new_buttons)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        yield {"type": "done", "answer": msg.content or "I could not finish that. Please try asking again.",
               "actions": actions, "suggestions": _unique_buttons(buttons)}
        return


def _ask_model(client, messages, tools, final=False):
    """One model call. On the last allowed step the model must answer in words, not call more tools."""
    if not final:
        msg = client.chat.completions.create(model=NRP_MODEL, messages=messages, tools=tools,
                                             max_tokens=STEP_TOKENS).choices[0].message
        if not msg.tool_calls and not (msg.content or "").strip():
            # a reasoning model can spend the whole budget thinking and return nothing: ask once more, directly
            retry = messages + [{"role": "system", "content": "Your last reply was empty. Reply now: call the tool "
                                                              "you need, or answer in a few sentences."}]
            msg = client.chat.completions.create(model=NRP_MODEL, messages=retry, tools=tools,
                                                 max_tokens=STEP_TOKENS).choices[0].message
        return msg
    nudge = messages + [{"role": "system", "content": "Stop using tools. Answer the user now with what you "
                                                      "have found so far, and say what is still unfinished."}]
    try:
        return client.chat.completions.create(model=NRP_MODEL, messages=nudge, tools=tools, tool_choice="none",
                                              max_tokens=STEP_TOKENS).choices[0].message
    except Exception:
        return client.chat.completions.create(model=NRP_MODEL, messages=nudge, max_tokens=STEP_TOKENS).choices[0].message


def run_agent(history, current_url="", user=None):
    """Returns (answer, last screen action) like before; see run_agent_events for everything."""
    answer, actions = "", []
    for event in run_agent_events(history, current_url, user):
        if event["type"] == "done":
            answer, actions = event["answer"], event["actions"]
    return answer, (actions[-1] if actions else None)


def run_agent_openai(history, current_url=""):
    return run_agent(history, current_url)


@login_required
@require_POST
def askadma_view(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
        history = data.get("messages")
        current_url = str(data.get("current_url", ""))[:500]
        if not history:
            q = (data.get("question") or "").strip()
            history = [{"role": "user", "content": q}] if q else []
    except Exception:
        return JsonResponse({"error": "Invalid request body"}, status=400)
    if not _clean_history(history):
        return JsonResponse({"error": "No question provided"}, status=400)

    if data.get("stream"):
        # Newline-delimited JSON: progress steps as they happen, then the answer.
        def events():
            try:
                for event in run_agent_events(history, current_url, request.user):
                    yield json.dumps(event, default=str) + "\n"
            except Exception as e:
                yield json.dumps({"type": "error", "error": str(e)}) + "\n"
        response = StreamingHttpResponse(events(), content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"   # tell nginx to pass each line on immediately
        return response

    try:
        answer, actions, buttons = "", [], []
        for event in run_agent_events(history, current_url, request.user):
            if event["type"] == "done":
                answer, actions, buttons = event["answer"], event["actions"], event["suggestions"]
        payload = {"answer": answer}
        if actions:
            payload["action"] = actions[-1]
            payload["actions"] = actions
        if buttons:
            payload["suggestions"] = buttons
        return JsonResponse(payload)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


THUMB_DIR = "/tmp/adma_thumbs"


@login_required
def askadma_thumb_view(request, file_id):
    """A JPEG preview of an image file, at most `w` pixels wide/tall, for the chat panel."""
    from PIL import Image, ImageOps
    fi = get_object_or_404(File, id=file_id)
    if fi.owner_id != request.user.id and not fi.is_public:
        return HttpResponseForbidden("Not allowed.")
    if fi.file_type != "image":
        raise Http404("Not an image.")
    try:
        width = max(64, min(int(request.GET.get("w", 320)), 2048))
    except (TypeError, ValueError):
        width = 320
    stamp = int(fi.updated_at.timestamp()) if fi.updated_at else 0
    path = os.path.join(THUMB_DIR, f"{fi.id}_{width}_{stamp}.jpg")
    if not os.path.exists(path):
        try:
            with fi.open_content("rb") as fh:
                img = Image.open(fh)
                img = ImageOps.exif_transpose(img)
                img.thumbnail((width, width))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                os.makedirs(THUMB_DIR, exist_ok=True)
                tmp = f"{path}.{threading.get_ident()}.tmp"
                img.save(tmp, format="JPEG", quality=82)
                os.replace(tmp, path)
        except Exception:
            raise Http404("Could not make a preview.")
    response = FileResponse(open(path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=86400"
    return response
