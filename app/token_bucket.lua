-- KEYS[1] = redis key for this user's bucket
-- ARGV[1] = max_tokens (5)
-- ARGV[2] = refill_rate_seconds (2, i.e. 1 token per 2 sec)
-- ARGV[3] = current_timestamp

local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local refill_interval = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call("HMGET", key, "tokens", "last_refill")
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    -- First request ever for this user
    tokens = max_tokens
    last_refill = now
end

-- Calculate how many tokens to add since last refill
local elapsed = now - last_refill
local tokens_to_add = math.floor(elapsed / refill_interval)

if tokens_to_add > 0 then
    tokens = math.min(max_tokens, tokens + tokens_to_add)
    last_refill = last_refill + (tokens_to_add * refill_interval)
end

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call("HMSET", key, "tokens", tokens, "last_refill", last_refill)
redis.call("EXPIRE", key, 60)  -- cleanup: agar user inactive ho jaye, key auto-delete ho

return allowed