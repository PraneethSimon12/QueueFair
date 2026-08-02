-- admit_batch.lua — release the next N waiters, at a rate no process can exceed.
--
-- INVARIANT THIS SCRIPT PROTECTS
--   Across ALL queue-service processes, at most `rate_per_min` waiters are admitted per minute
--   for this event, and each waiter is popped exactly once, ever.
--
-- THE INTERLEAVING IT PREVENTS (design.md §7)
--   Two processes each run an admission loop and wake at the same instant. Rate 100/min:
--       P1: read bucket -> 100 tokens        P2: read bucket -> 100 tokens
--       P1: ZRANGE 0..99 -> members 1..100   P2: ZRANGE 0..99 -> THE SAME members 1..100
--       P1: ZREM them, issue 100 passes      P2: ZREM (no-ops), issue 100 passes
--   Three distinct failures out of one race:
--     1. Over-admission — 200 released into a window sized for 100. The backpressure that is
--        this system's entire reason to exist has been bypassed and the booking service takes
--        double its designed load.
--     2. Double admission (FR-14) — one person holds two passes with two jti values. The
--        booking service's jti uniqueness cannot help: two jti are two different requests, so it
--        falls through to the (event, user_id) constraint and surfaces as a baffling 200 two
--        services away from the cause.
--     3. A corrupted `admitted` counter — incremented twice for one pop, which silently makes
--        EVERY waiter's position arithmetic (design.md §6) wrong.
--
-- WHY IT IS A SCRIPT — and the wrong answer to avoid giving
--   `ZPOPMIN key count` is already atomic, so the pop alone needs no Lua. The script exists
--   because the RATE CHECK AND THE POP MUST BE ATOMIC TOGETHER. "We used Lua because Redis
--   operations aren't atomic" is the wrong answer; they are, individually, and that is precisely
--   what does not save you here.
--
-- THE CONSEQUENCE WORTH NOTICING
--   Because the bucket is atomic, EVERY process can run this loop safely. There is no leader,
--   no Redis lock, no lease renewal, no failover detection, and no stall while a dead leader's
--   lease expires. Making the OPERATION atomic removes the need to make the ACTOR exclusive.
--
-- WHY now_ms IS AN ARGUMENT AND NOT redis.call('TIME')
--   It keeps the script a pure function of its inputs: testable with a fake clock, and
--   deterministic under replication. A script that reads the clock itself is neither.
--
-- KEYS[1] qf:{event}:queue      sorted set
-- KEYS[2] qf:{event}:admitted   counter
-- KEYS[3] qf:{event}:bucket     hash — tokens, last_refill_ms
-- KEYS[4] qf:{event}:config     hash — rate_per_min, burst, batch_max
-- ARGV[1] now_ms                caller's clock, milliseconds since epoch
--
-- RETURNS
--   {0}                                      event not configured
--   {1, count, admitted_total, {members...}} count may be 0; members is the flat ZPOPMIN reply
--                                            (member, score, member, score, ...)

if redis.call('EXISTS', KEYS[4]) == 0 then
    return {0}
end

local cfg = redis.call('HMGET', KEYS[4], 'rate_per_min', 'burst', 'batch_max')
local rate = tonumber(cfg[1]) or 0
local burst = tonumber(cfg[2]) or 0
local batch_max = tonumber(cfg[3]) or 0
local now = tonumber(ARGV[1])

-- rate 0 pauses the drop (FR-13). Return before touching the bucket: tokens already banked stay
-- banked, so resuming releases the burst that was saved up rather than starting from empty.
if rate <= 0 then
    return {1, 0, tonumber(redis.call('GET', KEYS[2]) or '0'), {}}
end

local state = redis.call('HMGET', KEYS[3], 'tokens', 'last_refill_ms')
local tokens = tonumber(state[1])
local last_refill = tonumber(state[2])

if tokens == nil or last_refill == nil then
    -- First call for this event. Start full, which is what `burst` means: the drop opens by
    -- letting a burst through, then settles to the steady rate.
    tokens = burst
    last_refill = now
end

local elapsed_ms = now - last_refill
if elapsed_ms < 0 then
    -- Clocks on two processes disagree, or one stepped backwards. Refilling by a negative
    -- interval would REMOVE tokens and stall the drop; treating it as zero merely wastes the
    -- interval. Under-admitting for one tick is recoverable; a stalled queue is not.
    elapsed_ms = 0
end

tokens = math.min(burst, tokens + elapsed_ms * rate / 60000)

-- Three separate ceilings, and each answers a different question:
--   floor(tokens) — may we, per the global rate?
--   batch_max     — how many will the booking service tolerate arriving at once?
--   ZCARD         — are there even that many people waiting?
local n = math.floor(tokens)
if n > batch_max then n = batch_max end
local waiting = redis.call('ZCARD', KEYS[1])
if n > waiting then n = waiting end

-- Written back even when n == 0, so the refill timestamp advances. Skipping this on an empty
-- queue would let `elapsed` accumulate unbounded and dump a huge batch the moment someone joins.
redis.call('HSET', KEYS[3], 'tokens', tokens - n, 'last_refill_ms', now)

if n <= 0 then
    return {1, 0, tonumber(redis.call('GET', KEYS[2]) or '0'), {}}
end

local popped = redis.call('ZPOPMIN', KEYS[1], n)
local admitted_total = redis.call('INCRBY', KEYS[2], n)

return {1, n, admitted_total, popped}
