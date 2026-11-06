# Lead Harvester

A standalone Python worker that collects public business leads and delivers them to a lead-management app over its API. It was built for the ClarioScope platform and is designed to run as an always-on Docker container.

## How it works

The worker polls the lead-management API for harvest jobs, runs the matching scraper, normalises each result into a common lead model, and sends the leads back. Each source is a separate scraper behind a shared base class and a registry, so adding a new source means adding one file.

## Sources

- Google Maps
- Yelp
- Healthgrades
- Opencare
- Psychology Today

## Project layout

- `lead-harvester/src/poller.py`: polls for jobs and dispatches them
- `lead-harvester/src/client.py`: API client for the lead-management app
- `lead-harvester/src/scrapers`: one scraper per source, with a base class and registry
- `lead-harvester/src/models.py` and `normalize.py`: the lead data model and cleanup
- `lead-harvester/mock_server.py`: a mock API for local testing
- `LEAD_HARVESTER_SPEC (1).md`: the integration specification
- `Roadmap.md`: the build roadmap

## Run it locally

```
cd lead-harvester
cp .env.example .env
docker compose up --build
```

Fill in `.env` first. It holds the API address, the shared access token, the polling interval and an optional proxy. Never commit the `.env` file.

## Stack

Python 3.11+, httpx, BeautifulSoup, Playwright, Pydantic v2, tenacity, Docker.

## Responsible use

Only collect publicly available business information, respect each site's terms of service and robots rules, and follow the privacy and anti-spam laws that apply to your outreach.
