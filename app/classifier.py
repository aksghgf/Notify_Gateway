import asyncio
import os
import json
import structlog
from groq import AsyncGroq
from pydantic import BaseModel
from typing import Literal
from dotenv import load_dotenv

load_dotenv()
logger = structlog.get_logger()

groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

class ClassificationResult(BaseModel):
    classification: Literal["urgent", "normal", "promotional"]

CLASSIFICATION_PROMPT = """You are a notification classifier. Classify the following message into exactly one category: urgent, normal, or promotional.

- urgent: Requires immediate attention (system failures, security alerts, critical deadlines)
- normal: Standard informational updates (status changes, confirmations)
- promotional: Marketing, offers, or non-essential announcements

Message: {message}
Source: {source}

Respond ONLY with valid JSON in this exact format: {{"classification": "urgent"}} or {{"classification": "normal"}} or {{"classification": "promotional"}}"""


async def classify_event(user_id: str, source: str, message: str) -> str:
    prompt = CLASSIFICATION_PROMPT.format(message=message, source=source)
    
    try:
        response = await asyncio.wait_for(
            groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            ),
            timeout=3.0
        )
        
        raw_output = response.choices[0].message.content
        parsed = json.loads(raw_output)
        result = ClassificationResult(**parsed)
        
        logger.info(
            "classification_success",
            user_id=user_id,
            source=source,
            classification=result.classification,
            raw_output=raw_output,
        )
        
        return result.classification
    
    except asyncio.TimeoutError:
        logger.warning(
            "classification_timeout",
            user_id=user_id,
            source=source,
            fallback="normal",
        )
        return "normal"
    
    except Exception as e:
        logger.error(
            "classification_failed",
            user_id=user_id,
            source=source,
            error=str(e),
            fallback="normal",
        )
        return "normal"