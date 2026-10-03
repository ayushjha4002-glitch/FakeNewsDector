import os
import json
import requests
from groq import Groq
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------- Setup ----------

# Reads the .env file so GROQ_API_KEY becomes available
load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise RuntimeError("GROQ_API_KEY not found. Check your .env file.")

client = Groq(api_key=api_key)

# The model name used in Groq's own quickstart guide
MODEL = "openai/gpt-oss-120b"

WIKI_URL = "https://en.wikipedia.org/w/api.php"
# Wikipedia asks API users to identify themselves with a User-Agent
HEADERS = {"User-Agent": "ClaimCheckerLearningProject/1.0 (student project)"}

app = FastAPI()


# ---------- Helper functions ----------

def ask_llm(prompt):
    """Sends one prompt to the AI and returns the text of the answer."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,  # 0 = more consistent answers, good for fact-checking
    )
    return response.choices[0].message.content

def parse_json(text):
    """
    The AI is told to reply with JSON only, but sometimes it adds extra
    words or ```json fences. This grabs everything from the first { to
    the last } and converts it into a Python dictionary.
    """
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("The AI did not return JSON: " + text)
    return json.loads(text[start:end])


def extract_claims(text):
    """Step 1: ask the AI to pull out checkable factual claims."""
    prompt = """You are a fact-checking assistant.
Read the text below and extract up to 3 specific, checkable factual claims.
Ignore opinions and predictions.
For each claim, also write a short search query (2 to 6 words) that would
work well for searching Wikipedia.

Reply with ONLY JSON in exactly this shape, and nothing else:
{"claims": [{"claim": "...", "search_query": "..."}]}

If there are no checkable claims, reply with: {"claims": []}

TEXT:
""" + text
    answer = ask_llm(prompt)
    data = parse_json(answer)
    return data["claims"]


def search_wikipedia(query):
    """Step 2: find evidence. Returns a list of {title, text, url}."""
    # First request: search for matching article titles
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 3,
        "format": "json",
    }
    r = requests.get(WIKI_URL, params=search_params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    results = r.json()["query"]["search"]
    titles = [item["title"] for item in results]

    if not titles:
        return []

    # Second request: get the intro text of those articles
    extract_params = {
        "action": "query",
        "prop": "extracts",
        "exintro": 1,        # only the introduction section
        "explaintext": 1,    # plain text instead of HTML
        "exlimit": 3,
        "titles": "|".join(titles),
        "format": "json",
    }
    r = requests.get(WIKI_URL, params=extract_params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    pages = r.json()["query"]["pages"]

    evidence = []
    for page in pages.values():
        text = page.get("extract", "")
        if text:
            evidence.append({
                "title": page["title"],
                "text": text[:1500],  # keep it short so the prompt stays small
                "url": "https://en.wikipedia.org/wiki/" + page["title"].replace(" ", "_"),
            })
    return evidence


def verify_claim(claim, evidence):
    """Step 3: ask the AI to judge the claim using ONLY the evidence."""
    if not evidence:
        return {
            "verdict": "NOT_ENOUGH_INFO",
            "explanation": "No Wikipedia evidence was found for this claim.",
            "source": None,
        }

    evidence_text = ""
    for i, item in enumerate(evidence):
        evidence_text += "[" + str(i + 1) + "] " + item["title"] + ": " + item["text"] + "\n\n"

    prompt = """You are a careful fact-checker.
Judge the CLAIM using ONLY the EVIDENCE below. Do not use outside knowledge.

Verdict options:
- SUPPORTED: the evidence clearly confirms the claim
- CONTRADICTED: the evidence clearly shows the claim is wrong
- NOT_ENOUGH_INFO: the evidence does not clearly settle it

If you are unsure, choose NOT_ENOUGH_INFO.

Reply with ONLY JSON in exactly this shape, and nothing else:
{"verdict": "SUPPORTED", "explanation": "one or two sentences", "source_number": 1}
Use null for source_number if no single source settles it.

CLAIM:
""" + claim + """

EVIDENCE:
""" + evidence_text
    answer = ask_llm(prompt)
    data = parse_json(answer)

    # Turn source_number into the actual article (title + link)
    source = None
    number = data.get("source_number")
    if isinstance(number, int) and 1 <= number <= len(evidence):
        chosen = evidence[number - 1]
        source = {"title": chosen["title"], "url": chosen["url"]}

    verdict = data.get("verdict")
    if verdict not in ("SUPPORTED", "CONTRADICTED", "NOT_ENOUGH_INFO"):
        verdict = "NOT_ENOUGH_INFO"

    return {
        "verdict": verdict,
        "explanation": data.get("explanation", ""),
        "source": source,
    }


# ---------- API routes ----------

class CheckRequest(BaseModel):
    text: str


@app.get("/")
def home():
    """Serves our frontend page."""
    return FileResponse("index.html")


@app.post("/check")
def check(req: CheckRequest):
    text = req.text.strip()
    if len(text) < 10:
        raise HTTPException(status_code=400, detail="Please enter a longer text.")
    if len(text) > 3000:
        raise HTTPException(status_code=400, detail="Text is too long (max 3000 characters).")

    try:
        claims = extract_claims(text)
        results = []
        for item in claims:
            evidence = search_wikipedia(item["search_query"])
            judged = verify_claim(item["claim"], evidence)
            results.append({
                "claim": item["claim"],
                "verdict": judged["verdict"],
                "explanation": judged["explanation"],
                "source": judged["source"],
            })
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Something went wrong: " + str(e))