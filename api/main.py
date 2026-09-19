import os
import time
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator


TAVILY_URL = "https://api.tavily.com/search"
DEFAULT_ORIGINS = "https://enu20123-cell.github.io,http://localhost:8000,http://127.0.0.1:8000"


class Profile(BaseModel):
    goalText: str = Field(min_length=8, max_length=900)
    countries: list[str] = Field(default_factory=list, max_length=12)
    interest: str = Field(min_length=2, max_length=100)
    gpa: float = Field(default=0, ge=0, le=5)
    ielts: float = Field(default=0, ge=0, le=9)
    sat: int = Field(default=0, ge=0, le=1600)
    budget: int = Field(default=0, ge=0, le=200_000)
    intake: str = Field(min_length=4, max_length=10)

    @field_validator("goalText", "interest", "intake")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(str(value).split())

    @field_validator("countries")
    @classmethod
    def clean_countries(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(" ".join(str(item).split())[:60] for item in value if str(item).strip()))


app = FastAPI(title="ORBIT AI API", version="1.0.0")
origins = [item.strip().rstrip("/") for item in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

requests_by_ip: dict[str, deque[float]] = defaultdict(deque)
cache: dict[str, tuple[float, dict[str, Any]]] = {}

COUNTRY_TLDS = {
    "Netherlands": ".nl", "Germany": ".de", "Finland": ".fi", "Estonia": ".ee",
    "Hungary": ".hu", "Canada": ".ca", "USA": ".us", "UK": ".uk",
    "Spain": ".es", "France": ".fr", "Italy": ".it", "Poland": ".pl",
    "Austria": ".at", "Czechia": ".cz", "Sweden": ".se", "Norway": ".no",
    "UAE": ".ae", "South Korea": ".kr", "Japan": ".jp", "Australia": ".au",
    "New Zealand": ".nz", "Switzerland": ".ch", "Belgium": ".be",
    "Denmark": ".dk", "Ireland": ".ie", "Portugal": ".pt", "Greece": ".gr",
    "Turkey": ".tr", "Singapore": ".sg", "China": ".cn", "Malaysia": ".my",
    "Hong Kong": ".hk", "Qatar": ".qa", "Saudi Arabia": ".sa",
    "Kazakhstan": ".kz", "Lithuania": ".lt", "Latvia": ".lv",
    "Romania": ".ro", "Bulgaria": ".bg", "Cyprus": ".cy",
}

ACADEMIC_CONTEXT_TERMS = (
    "university", "università", "universita", "universität", "universite",
    "université", "universidad", "universidade", "universiteit", "universitet",
    "uniwersytet", "college", "institute", "polytechnic", "faculty", "school of",
    "admission", "bachelor", "undergraduate", "degree", "computer science",
)


def academic_domain(host: str, countries: Optional[list[str]] = None, context: str = "") -> bool:
    blocked = (
        "youtube.com", "wikipedia.org", "reddit.com", "facebook.com", "instagram.com",
        "tiktok.com", "pinterest.", "medium.com", "globaladmissions.com",
        "bachelorsportal.com", "mastersportal.com", "studyportals.com",
        "educations.com", "topuniversities.com", "studyabroad.com", "applyboard.com",
        "mygermanuniversity.com", "studying-in-germany.org",
    )
    if not host or any(item in host for item in blocked):
        return False
    if host.endswith((".edu", ".gov")) or ".edu." in host or ".ac." in host or ".gov." in host:
        return True
    if any(word in host for word in (
        "university", "universit", "college", "institute", "institut", "polytechnic",
        "polytech", "studyin", "studywith", "ucas", "daad", "nuffic", "uni-assist",
    )):
        return True

    # Many official European university domains are abbreviations (for example
    # unibo.it, polimi.it or tum.de) and contain no English academic keyword.
    # Accept such results only when Tavily's title/snippet is academic and the
    # domain uses the selected country's national suffix.
    expected_suffixes = {
        COUNTRY_TLDS[country] for country in (countries or []) if country in COUNTRY_TLDS
    }
    normalized_context = " ".join(context.lower().split())
    return bool(
        expected_suffixes
        and any(host.endswith(suffix) for suffix in expected_suffixes)
        and any(term in normalized_context for term in ACADEMIC_CONTEXT_TERMS)
    )


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.").lower()
    except ValueError:
        return ""


def make_query(profile: Profile) -> str:
    destination = ", ".join(profile.countries) or profile.goalText
    return " ".join((
        f"Target location exactly: {destination}. Only recommend universities physically located in this target country or region.",
        f"{profile.interest} bachelor international admissions IELTS tuition application deadline official university {profile.intake}.",
        f"Applicant GPA {profile.gpa}/5, IELTS {profile.ielts or 'not taken'}, SAT {profile.sat or 'not taken'}, tuition budget USD {profile.budget}.",
        "Use only facts supported by the search sources. Do not invent exact tuition, scores or deadlines. If a fact is missing, say it must be verified.",
        "Recommend 4 to 5 relevant undergraduate programs when evidence is available; do not confuse bachelor, master or transfer requirements.",
        "Return four clearly separated sections with these exact Russian headings: ВУЗЫ И ПРОГРАММЫ, КРИТЕРИИ ОЦЕНКИ, ПЕРСОНАЛЬНЫЙ ПЛАН, ЧТО НУЖНО ПРОВЕРИТЬ.",
        "In ВУЗЫ И ПРОГРАММЫ number every bachelor's option as 1), 2), 3). Separate criteria and action steps with semicolons; include 0–30 and 30–90 day actions. Do not use markdown tables.",
    ))


async def tavily(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="TAVILY_API_KEY не настроен")
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code in (429, 432, 433):
        raise HTTPException(status_code=429, detail="Квота AI-поиска временно исчерпана")
    if response.is_error:
        raise HTTPException(status_code=502, detail="AI-поиск временно недоступен")
    return response.json()


def enforce_rate_limit(request: Request) -> None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "anonymous")
    now = time.time()
    attempts = requests_by_ip[ip]
    while attempts and now - attempts[0] > 600:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте через несколько минут")
    attempts.append(now)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True, "aiConfigured": bool(os.getenv("TAVILY_API_KEY", "").strip())}


@app.post("/api/plan")
async def create_plan(profile: Profile, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request)
    cache_key = profile.model_dump_json()
    now = time.time()
    cached = cache.get(cache_key)
    if cached and now - cached[0] < 1_200:
        return cached[1]

    destination = " ".join(profile.countries) or profile.goalText
    discovery = await tavily({
        "query": f"{destination} университет {profile.interest} бакалавриат поступление официальный сайт. {destination} official university {profile.interest} bachelor admission.",
        "topic": "general", "search_depth": "advanced", "chunks_per_source": 1,
        "max_results": 20, "include_answer": False, "include_raw_content": False,
        "include_images": False, "include_usage": True,
    })
    trusted = list(dict.fromkeys(
        host for item in discovery.get("results", [])
        if (host := host_of(str(item.get("url", "")))) and academic_domain(
            host,
            profile.countries,
            f"{item.get('title', '')} {item.get('content', '')}",
        )
    ))[:40]
    if not trusted:
        raise HTTPException(status_code=502, detail="Не найден официальный университетский источник. Уточните страну или направление")

    result = await tavily({
        "query": make_query(profile), "topic": "general", "search_depth": "advanced",
        "chunks_per_source": 2, "max_results": 10, "include_answer": "advanced",
        "include_raw_content": False, "include_images": False, "include_favicon": True,
        "include_usage": True, "include_domains": trusted, "include_domains_mode": "filter",
    })
    sources = []
    for item in result.get("results", []):
        url = str(item.get("url", ""))
        host = host_of(url)
        if url.startswith("https://") and (host in trusted or academic_domain(host)):
            sources.append({
                "title": " ".join(str(item.get("title", "Официальный источник")).split())[:180],
                "url": url,
                "publishedDate": str(item.get("published_date", ""))[:40],
                "score": float(item.get("score", 0) or 0),
            })
    answer = str(result.get("answer", "")).strip()[:9_000]
    sources = sources[:8]
    if not answer or len(sources) < 2:
        raise HTTPException(status_code=502, detail="Недостаточно надёжных источников. Уточните запрос")

    value = {
        "mode": "live", "answer": answer, "sources": sources,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "credits": int(discovery.get("usage", {}).get("credits", 0) or 0) + int(result.get("usage", {}).get("credits", 0) or 0),
    }
    cache[cache_key] = (now, value)
    if len(cache) > 80:
        cache.pop(next(iter(cache)))
    return value
