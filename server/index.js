const PAGE = __ORBIT_PAGE__;
const recent = new Map();
const cache = new Map();

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff"
  }
});

function cleanText(value, max = 700) {
  return String(value ?? "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, max);
}

function cleanNumber(value, min, max) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : 0;
}

function validUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

function profileFrom(input) {
  const countries = Array.isArray(input?.countries) ? input.countries.slice(0, 12).map(x => cleanText(x, 60)).filter(Boolean) : [];
  return {
    goalText: cleanText(input?.goalText, 900),
    countries,
    interest: cleanText(input?.interest, 100),
    gpa: cleanNumber(input?.gpa, 0, 5),
    ielts: cleanNumber(input?.ielts, 0, 9),
    sat: cleanNumber(input?.sat, 0, 1600),
    budget: cleanNumber(input?.budget, 0, 200000),
    intake: cleanText(input?.intake, 10)
  };
}

function makeQuery(p) {
  const destination = p.countries.length ? p.countries.join(", ") : p.goalText;
  return [
    "Target location exactly: " + destination + ". Only recommend universities physically located in this target country or region.",
    p.interest + " bachelor international admissions IELTS tuition application deadline official university " + p.intake + ".",
    "Applicant GPA " + p.gpa + "/5, IELTS " + (p.ielts || "not taken") + ", SAT " + (p.sat || "not taken") + ", tuition budget USD " + p.budget + ".",
    "Use only facts supported by the search sources. Do not invent exact tuition, scores or deadlines. If a fact is missing, say it must be verified.",
    "Recommend 4 to 5 relevant undergraduate programs when evidence is available; do not confuse bachelor, master or transfer requirements.",
    "Ответ по-русски: ВУЗЫ И ПРОГРАММЫ, КРИТЕРИИ ОЦЕНКИ, ПЕРСОНАЛЬНЫЙ ПЛАН 0–30 и 30–90 дней, ЧТО НУЖНО ПРОВЕРИТЬ."
  ].join(" ");
}

function domainOf(value) {
  try { return new URL(value).hostname.replace(/^www./, "").toLowerCase(); }
  catch { return null; }
}

function academicDomain(host) {
  if (!host) return false;
  const blocked = ["youtube.com", "wikipedia.org", "reddit.com", "facebook.com", "instagram.com", "tiktok.com", "pinterest.", "medium.com", "globaladmissions.com", "bachelorsportal.com", "mastersportal.com", "studyportals.com", "educations.com", "topuniversities.com", "studyabroad.com", "applyboard.com"];
  if (blocked.some(domain => host.includes(domain))) return false;
  if (host.endsWith(".edu") || host.includes(".edu.") || host.includes(".ac.") || host.endsWith(".gov") || host.includes(".gov.")) return true;
  return ["university", "universit", "college", "institute", "institut", "polytechnic", "polytech", "studyin", "studywith", "ucas", "daad", "nuffic", "uni-assist"].some(word => host.includes(word));
}

async function tavilySearch(env, body) {
  const response = await fetch("https://api.tavily.com/search", {
    method: "POST",
    headers: { "authorization": "Bearer " + env.TAVILY_API_KEY, "content-type": "application/json" },
    body: JSON.stringify(body)
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error("Tavily request failed");
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function plan(request, env) {
  if (!env.TAVILY_API_KEY) return json({ error: "Бесплатный AI-ключ ещё не подключён", code: "missing_api_key" }, 503);
  const type = request.headers.get("content-type") || "";
  if (!type.includes("application/json")) return json({ error: "Ожидается JSON" }, 415);
  let input;
  try { input = await request.json(); } catch { return json({ error: "Некорректный запрос" }, 400); }
  const profile = profileFrom(input);
  if (profile.goalText.length < 8 || !profile.interest) return json({ error: "Опишите цель и направление подробнее" }, 400);

  const ip = request.headers.get("cf-connecting-ip") || "anonymous";
  const now = Date.now();
  const attempts = (recent.get(ip) || []).filter(time => now - time < 600000);
  if (attempts.length >= 3) return json({ error: "Слишком много запросов. Попробуйте через несколько минут." }, 429);
  attempts.push(now);
  recent.set(ip, attempts);

  const query = makeQuery(profile);
  const key = JSON.stringify(profile);
  const cached = cache.get(key);
  if (cached && now - cached.time < 1200000) return json(cached.value);

  const destination = profile.countries.length ? profile.countries.join(" ") : profile.goalText;
  let discovery;
  let payload;
  let trustedDomains = [];
  try {
    discovery = await tavilySearch(env, {
      query: destination + " университет " + profile.interest + " бакалавриат поступление официальный сайт. " + destination + " official university " + profile.interest + " bachelor admission.",
      topic: "general",
      search_depth: "advanced",
      chunks_per_source: 1,
      max_results: 20,
      include_answer: false,
      include_raw_content: false,
      include_images: false,
      include_usage: true
    });
    const discoveredDomains = Array.isArray(discovery.results) ? discovery.results.map(result => domainOf(result.url)).filter(academicDomain) : [];
    trustedDomains = Array.from(new Set(discoveredDomains)).slice(0, 40);
    if (trustedDomains.length < 1) return json({ error: "Не удалось найти официальный университетский источник. Уточните страну или направление." }, 502);
    payload = await tavilySearch(env, {
      query,
      topic: "general",
      search_depth: "advanced",
      chunks_per_source: 2,
      max_results: 10,
      include_answer: "advanced",
      include_raw_content: false,
      include_images: false,
      include_favicon: true,
      include_usage: true,
      include_domains: trustedDomains,
      include_domains_mode: "filter"
    });
  } catch (error) {
    const limited = error.status === 429 || error.status === 432 || error.status === 433;
    return json({ error: limited ? "Бесплатная квота временно исчерпана" : "AI-поиск временно недоступен" }, limited ? 429 : 502);
  }

  const rawSources = Array.isArray(payload.results) ? payload.results.map(result => ({
    title: cleanText(result.title, 180),
    url: validUrl(result.url),
    publishedDate: cleanText(result.published_date, 40),
    score: Number(result.score) || 0
  })).filter(source => source.url) : [];
  const sources = rawSources.filter(source => {
    const host = domainOf(source.url);
    return trustedDomains.includes(host) || academicDomain(host);
  }).slice(0, 8);
  const answer = cleanText(payload.answer, 9000);
  if (!answer || sources.length < 2) return json({ error: "AI не нашёл достаточно надёжных источников. Измените страну или уточните направление." }, 502);
  const value = {
    mode: "live",
    answer,
    sources,
    generatedAt: new Date().toISOString(),
    credits: Number(discovery?.usage?.credits || 0) + Number(payload.usage?.credits || 0)
  };
  cache.set(key, { time: now, value });
  if (cache.size > 80) cache.delete(cache.keys().next().value);
  return json(value);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/api/plan") return plan(request, env);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(PAGE, { headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-cache",
        "x-content-type-options": "nosniff",
        "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=()"
      }});
    }
    if (request.method === "GET" && url.pathname === "/health") return json({ ok: true, aiConfigured: Boolean(env.TAVILY_API_KEY) });
    return new Response("Not found", { status: 404 });
  }
};
