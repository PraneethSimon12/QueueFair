-- join.lua — place a waiter in the queue, exactly once, ever.
--
-- INVARIANT THIS SCRIPT PROTECTS
--   1. A queue token is assigned an arrival sequence exactly once. Joining again returns the
--      sequence it already has and changes nothing. This is fairness promise F2/F3: refreshing,
--      double-tapping or reconnecting can never alter your place.
--   2. Sequences for an event are DENSE — no gaps, ever. This one is easy to treat as cosmetic
--      and is not: design.md §6 computes position as `my_seq - admitted`, which is only correct
--      if every number between 1 and the current sequence was actually handed to somebody. Each
--      gap inflates every later waiter's displayed position, permanently, and silently.
--
-- WHY IT MUST BE ONE SCRIPT
--   The obvious implementation is ZSCORE, then INCR, then ZADD as three round trips. Two joins
--   with the same token interleave like this (design.md §5):
--       A: ZSCORE -> nil          B: ZSCORE -> nil
--       A: INCR -> 100            A: ZADD tok 100
--       B: INCR -> 101            B: ZADD tok 101     <- overwrites; the user's place got WORSE
--   Both read before either wrote, and plain ZADD overwrites. The user's number goes up because
--   they double-tapped, with no error and no log line anywhere.
--
--   ZADD NX alone fixes the fairness half — B's ZADD becomes a no-op — but B's INCR to 101 is
--   still burned, so the sequence develops a gap and invariant 2 breaks. The gaps, not the round
--   trips, are why this is a script: performance is the smaller argument here.
--
--   Redis is single-threaded and runs a script to completion before serving any other command,
--   so no other client can observe the state between the ZSCORE and the ZADD. The window the
--   race needs does not exist.
--
-- KEYS[1] qf:{event}:queue      sorted set — member = queue token, score = arrival sequence
-- KEYS[2] qf:{event}:seq        string counter — the only source of ordering (design.md §3)
-- KEYS[3] qf:{event}:admitted   string counter — total ever admitted
-- KEYS[4] qf:{event}:config     hash — rate_per_min, burst, batch_max
-- ARGV[1] queue token, 32 lowercase hex characters
--
-- RETURNS
--   {0}                                                    event is not configured -> HTTP 404
--   {1, seq, joined, total_waiting, admitted, rate_per_min} joined = 1 new placement, 0 resume
--
-- Everything the caller needs comes back in this one reply. That is deliberate: build-plan §5
-- budgets POST /join at exactly ONE Redis round trip, and reading the queue depth or the
-- admitted counter afterwards would quietly make it three.

if redis.call('EXISTS', KEYS[4]) == 0 then
    return {0}
end

local joined = 0
local seq = redis.call('ZSCORE', KEYS[1], ARGV[1])

if seq then
    -- Already queued. Return the existing sequence untouched — no INCR, so no number is burned
    -- and invariant 2 holds even under a client that retries forever.
    seq = tonumber(seq)
else
    -- INCR only on the path that actually adds a member, so every sequence issued is used.
    seq = redis.call('INCR', KEYS[2])
    redis.call('ZADD', KEYS[1], seq, ARGV[1])
    joined = 1
end

-- `or '0'` because a counter that has never been written returns false in Lua, not nil-as-zero.
local admitted = tonumber(redis.call('GET', KEYS[3]) or '0')
local rate = tonumber(redis.call('HGET', KEYS[4], 'rate_per_min') or '0')

return {1, seq, joined, redis.call('ZCARD', KEYS[1]), admitted, rate}
