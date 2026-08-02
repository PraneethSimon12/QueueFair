-- position.lua — the authoritative answer to "where am I?"
--
-- INVARIANT THIS SCRIPT PROTECTS
--   Every number in one response describes the SAME instant. Position, queue depth and the
--   admitted counter are read inside one script, so no admission batch can land between them.
--
--   Why that matters more than it sounds: without atomicity a response can mix a ZRANK taken
--   before a batch with an admitted counter taken after it. The waiter is then shown a position
--   computed from two different moments — and the direction of the error is not random. It can
--   make the number go UP, which is the single thing product-spec F4 promises never happens and
--   the thing users notice fastest.
--
-- WHY ZRANK AND NOT `my_seq - admitted`
--   The arithmetic in design.md §6 is what the SSE path will use, because it costs no Redis call
--   at all. It is an OPTIMISATION over this script, and it can drift: it assumes nobody ever
--   abandons the queue, and every abandonment leaves someone's computed position one too high
--   forever. ZRANK cannot drift — it counts the members actually ahead of you. This endpoint is
--   therefore the reference the arithmetic is reconciled AGAINST (§6, and the qf_position_drift
--   metric), which is why it survives after SSE lands rather than being deleted.
--
-- ONE ROUND TRIP, NOT THE TWO IN build-plan §5
--   §5 budgeted ZSCORE + ZRANK. ZRANK alone answers both questions — it returns the rank, or
--   nil when the member is absent — and folding ZCARD, the admitted counter and the rate into
--   the same script makes the whole response one call instead of four. Beating the budget was
--   not the reason; the atomic snapshot above was. The round trip is the bonus.
--
-- LEAVING THE QUEUE IS NOT THE SAME AS NEVER HAVING BEEN IN IT
--   Admission removes a waiter from the sorted set, so from ZRANK's point of view an admitted
--   person and a stranger are identical: both have no rank. Answering both with "unknown token"
--   would tell someone who just reached the front that they are not in the queue — the single
--   most alarming thing this system could say, and at the worst possible moment. So a missing
--   rank falls through to the pass key before we conclude anything.
--
-- KEYS[1] qf:{event}:queue           sorted set
-- KEYS[2] qf:{event}:admitted        counter
-- KEYS[3] qf:{event}:config          hash
-- KEYS[4] qf:{event}:pass:{token}    string, the admission payload, TTL = the pass lifetime
-- ARGV[1] queue token, 32 lowercase hex characters
--
-- RETURNS
--   {0}                                                    event not configured   -> HTTP 404
--   {1}                                          never queued, or pass expired    -> HTTP 404
--   {2, position, total_waiting, admitted, rate_per_min}   still waiting          -> HTTP 200
--   {3, admission_json, total_waiting, admitted}           admitted, pass waiting -> HTTP 200

if redis.call('EXISTS', KEYS[3]) == 0 then
    return {0}
end

local rank = redis.call('ZRANK', KEYS[1], ARGV[1])
if not rank then
    local admission = redis.call('GET', KEYS[4])
    if admission then
        return {
            3,
            admission,
            redis.call('ZCARD', KEYS[1]),
            tonumber(redis.call('GET', KEYS[2]) or '0'),
        }
    end
    -- No rank and no pass. Either this token was never queued, or it was admitted and its pass
    -- has since expired — which is `expired`, not `unknown`, but the two are indistinguishable
    -- once the key is gone. Phase 9's UI shows "your turn passed, rejoin", which is the honest
    -- reading of both (Journey D).
    return {1}
end

-- ZRANK is 0-based; humans count from 1, and "you are 0th in line" is not a sentence.
local position = rank + 1
local admitted = tonumber(redis.call('GET', KEYS[2]) or '0')
local rate = tonumber(redis.call('HGET', KEYS[3], 'rate_per_min') or '0')

return {2, position, redis.call('ZCARD', KEYS[1]), admitted, rate}
