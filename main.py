import os
import re
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig

app = FastAPI(title="Seam Transcript Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class TranscriptRequest(BaseModel):
    video_id: str


def format_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


async def fetch_video_metadata(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            html = resp.text
        title_match = re.search(r"<title>(.*?)</title>", html)
        raw_title = title_match.group(1) if title_match else ""
        video_title = raw_title.replace(" - YouTube", "").strip()
        channel_match = re.search(r'"channelName":"([^"]+)"', html)
        if not channel_match:
            channel_match = re.search(r'"ownerChannelName":"([^"]+)"', html)
        channel = channel_match.group(1) if channel_match else ""
        duration_match = re.search(r'"lengthSeconds":"(\d+)"', html)
        duration_secs = int(duration_match.group(1)) if duration_match else 0
        duration = format_duration(duration_secs) if duration_secs else ""
        return {"videoTitle": video_title, "channel": channel, "duration": duration}
    except Exception:
        return {"videoTitle": "", "channel": "", "duration": ""}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcript")
async def get_transcript(req: TranscriptRequest):
    video_id = req.video_id.strip()

    if not video_id or not re.match(r"^[A-Za-z0-9_-]{11}$", video_id):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_id", "message": "Invalid YouTube video ID."},
        )

    loop = asyncio.get_event_loop()
    transcript_result = None
    transcript_error = None
    raw_exception = None

    async def fetch_transcript():
        nonlocal transcript_result, transcript_error, raw_exception

        # Initialize API with proxy if available
        proxy_url = os.environ.get("PROXY_URL")
        proxy_config = None
        if proxy_url:
            proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)

        try:
            # v1.0: instantiate first, then call fetch() as instance method
            api = YouTubeTranscriptApi(proxy_config=proxy_config)
            fetched = await loop.run_in_executor(
                None,
                lambda: api.fetch(video_id, languages=["en", "en-US", "en-GB"]),
            )
            transcript_result = [
                {"text": s.text, "start": s.start, "duration": s.duration}
                for s in fetched
            ]
        except TranscriptsDisabled:
            transcript_error = {"status": 404, "error": "no_transcript", "message": "This video doesn't have captions available."}
        except NoTranscriptFound:
            try:
                api = YouTubeTranscriptApi(proxy_config=proxy_config)
                transcript_list = await loop.run_in_executor(
                    None,
                    lambda: api.list(video_id),
                )
                first = next(iter(transcript_list))
                fetched = await loop.run_in_executor(None, lambda: first.fetch())
                transcript_result = [
                    {"text": s.text, "start": s.start, "duration": s.duration}
                    for s in fetched
                ]
            except Exception as inner_e:
                raw_exception = f"NoTranscriptFound fallback: {type(inner_e).__name__}: {str(inner_e)}"
                transcript_error = {"status": 404, "error": "no_transcript", "message": "This video doesn't have captions available."}
        except VideoUnavailable:
            transcript_error = {"status": 403, "error": "restricted", "message": "This video is restricted and can't be processed."}
        except Exception as e:
            raw_exception = f"{type(e).__name__}: {str(e)}"
            err_str = str(e).lower()
            if "age" in err_str or "sign in" in err_str:
                transcript_error = {"status": 403, "error": "restricted", "message": "This video is restricted and can't be processed."}
            else:
                transcript_error = {"status": 500, "error": "fetch_failed", "message": "Couldn't fetch the transcript. Try again in a moment."}

    results = await asyncio.gather(fetch_transcript(), fetch_video_metadata(video_id))
    metadata = results[1]

    if transcript_error:
        detail = {"error": transcript_error["error"], "message": transcript_error["message"]}
        if raw_exception:
            detail["debug"] = raw_exception
        raise HTTPException(status_code=transcript_error["status"], detail=detail)

    return {
        "transcript": transcript_result,
        "videoTitle": metadata["videoTitle"],
        "channel": metadata["channel"],
        "duration": metadata["duration"],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
