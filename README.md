# NotifyGate

A rate-limited, LLM-classified real-time notification gateway. Built with FastAPI, Redis (Upstash), WebSockets, and Groq.

---

## Architecture

![NotifyGate architecture](./docs/architecture.svg)

### How cross-instance delivery works

The tricky requirement here was: a user connected to Instance A needs to get an event even if that event came in through Instance B. Here's how I approached it.

Each instance keeps its own in-memory `user_id → WebSocket` dictionary (`connection_manager.py`). That map only means anything inside that one process — I made sure never to treat it as the source of truth for anything.

When a client connects and sends its `user_id`, that instance also subscribes to a Redis Pub/Sub channel for that user (`channel:{user_id}`) in a background task. So now, whenever *any* instance ingests an event for that user, it just publishes to that same channel — it doesn't need to know or care who's actually holding the connection. Redis takes care of fanning that out to whichever instance is subscribed, and that instance forwards it to its local socket.

The nice part: `PUBLISH` tells you how many subscribers actually got the message. If that number is 0, I know nobody has this user connected anywhere, so I write the event to a Redis List instead so it isn't lost.

So Redis Pub/Sub is doing the cross-instance bridging, and the Redis List is the safety net for offline users. I didn't need to change anything about this design to make it work behind a load balancer with multiple instances — it was built that way from the start.

### How the rate limiter actually works

This is a token bucket, implemented as a Redis Lua script (`app/token_bucket.lua`) so the whole check-and-decrement happens as one atomic operation on Redis's side.

Here's the logic:

1. Each `user_id` gets a Redis hash storing two things: how many tokens it currently has, and when it was last refilled.
2. On every request, the script first figures out how much time has passed since the last refill, and calculates how many new tokens should've accumulated since then (1 every 2 seconds), capped at the max of 5.
3. If there's at least 1 token available after that, it deducts one and allows the request. If not, it rejects it.
4. It writes the updated token count and refill timestamp back, and sets a 60-second expiry on the key so idle users don't leave stale data sitting in Redis forever.

The reason I did this as a Lua script instead of separate GET/SET calls from Python is concurrency. If I read the token count, calculated the new value in Python, and then wrote it back — two requests arriving at the same time could both read "3 tokens left," both think they're allowed, and both write back 2, when really only one of them should've gone through. Redis runs Lua scripts as a single atomic step, so there's no window where two requests can interleave like that. That's what makes the "exactly 5 out of 20" test actually hold up under real concurrency instead of just usually working.

---

## Setup

You'll need Python 3.10+, a free [Upstash Redis](https://upstash.com) database, and a free [Groq API key](https://console.groq.com).

```bash
git clone <your-repo-url>
cd notifygate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and drop in your own values:
```
REDIS_URL=rediss://default:<password>@<your-endpoint>.upstash.io:6379
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxx
```

Run the server:
```bash
python -m uvicorn app.main:app --reload
```

Run the tests:
```bash
python -m pytest tests/ -v
```
This includes the concurrency test from Part 1 — it fires 20 simultaneous requests at the same `user_id` and checks that exactly 5 go through.

---

## Trying it out

For the REST endpoints, easiest way is to just go to `http://127.0.0.1:8000/docs` once the server's running — FastAPI gives you a working Swagger UI for free, so you can hit `/events`, `/health`, and `/notifications/{user_id}/pending` straight from the browser without needing curl or Postman.

WebSocket connections can't be tested from Swagger though, so I added a small script for that:
```bash
python tests/test_ws_client.py
```
Run that in one terminal to connect as a test user, then `POST /events` with the same `user_id` from another terminal — you should see the message show up instantly in the first one.

---

## Written Section

### If Redis goes down for 90 seconds mid-ingestion

Honestly, right now this would break pretty badly. Both the rate limiter and the publish/pending-store logic talk to Redis directly with no error handling around those calls, so if Redis is unreachable, that call just throws, and the whole `/events` request blows up into a `500`. Every request during the outage would fail the same way, and from the outside there's no way to tell "you got rate limited" apart from "the system is actually down" — they'd both just look broken.

The smallest fix I'd make is wrapping the Redis calls in try/except and deciding explicitly what happens when Redis isn't there. I'd lean toward failing closed for the rate limiter specifically — return a `503` instead of quietly letting everything through — since the entire point of that limiter is protection, and I'd rather be conservative than let a Redis blip turn into an accidental DDoS on my own downstream services. I'd also log that as its own thing (`redis_unavailable`) so it's not confused with a normal rate-limit rejection in the logs. A more robust version could fall back to some short-lived local counter while Redis is down, but that brings back the exact cross-instance inconsistency problem this whole design is trying to avoid, so for now I think fail-closed with clear logging is the more honest tradeoff.

### If the LLM provider errors out for 10 minutes straight

Walking through this one step by step: a request comes in, passes the rate limiter fine (that part doesn't touch the LLM at all), and then hits `classify_event()`. Because that call is wrapped in `asyncio.wait_for(timeout=3.0)`, it doesn't just hang — either Groq comes back with an error right away, or it gets cut off at 3 seconds. Both paths get logged (`classification_failed` or `classification_timeout`, with whatever error came back) and both fall back to returning `"normal"`.

The thing I want to be clear about: the event itself doesn't get dropped. It still goes through the rest of the pipeline exactly like normal — gets published, gets delivered live if the user's connected, or lands in the pending store if not. The only actual damage during the outage is that everything gets classified as `"normal"`, so something that's genuinely urgent would show up with the same priority as routine noise. That's a real problem, but I made that tradeoff on purpose — I'd rather deliver everything with the wrong priority than start silently dropping messages, because you can recover from a misclassification but you can't recover from a notification that never arrived.

### One thing I had to just decide on: missing user_id over WebSocket

For `POST /events`, if `user_id` is missing, Pydantic just handles it automatically and returns a `422` — I didn't have to think about it. But the WebSocket endpoint doesn't have that kind of validation built in; I'm reading the first message manually with `receive_json()`. So if someone connects and sends an empty or missing `user_id`, nothing would stop that from happening — it'd just get used as a dictionary key, effectively creating a connection that can never receive anything meaningful, and if a second broken client did the same thing, it'd silently overwrite the first one.

I decided to just check for `user_id` right after accepting the connection, and if it's missing, close the socket immediately with a custom close code (4001) and a reason string. Felt like the right call mainly for consistency — it's basically doing manually what Pydantic already does for free on the REST side, and it avoids that silent-overwrite bug completely.

---

## What I'd do differently with more time

- Actually handle Redis being unavailable instead of letting it 500 — as above
- Right now `GET /notifications/:user_id/pending` clears the list the moment it's read, which means if a client crashes right after getting the response but before actually processing it, that data's gone for good. I'd want some kind of ack step, or at least a short delay before deleting
- A local fallback rate limiter for short Redis outages, so it's not just all-or-nothing between fail-open and fail-closed
- I only tested the pub/sub cross-instance design manually with two terminals — I'd want to actually load test it with a bunch of concurrent connections to be more confident it holds up
- Adding a request ID that follows one event through the whole pipeline (rate limit → classify → deliver) in the logs. I actually ran into a confusing moment while building this where a stale reloaded server process made my test results look wrong — a request ID would've made that obvious in about five seconds instead of the twenty minutes it took me to figure out
