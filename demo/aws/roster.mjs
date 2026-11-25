// The conditional writes that decide who is in a round and when it closes. `store` offers
//   update(expression, condition, values)  -> boolean
//   claim(expression, condition, values)   -> the item after the write (ALL_NEW), or null
// so the ordering rules can be tested without DynamoDB.
export const s = value => ({ S: value });
export const n = value => ({ N: String(value) });

// Whole-roster write (join, rename, pick, remove). It lands only if nothing else wrote the roster
// since it was read, the run is the same, and either the game has not opened or the open round is
// still the round the writer saw. After Close (phase closing) or a later round it fails.
// It drops the previous round's frozen copy: a Close retry needs it only while the phase is closing,
// when no roster write can land, and keeping it would double the item every roster write pays for.
export const ROSTER_WRITE = {
  expression: 'SET teams_data = :next REMOVE frozen_data',
  condition: 'run_id = :id AND (phase IN (:ready, :starting) OR (phase = :open AND round = :round)) AND teams_data = :before',
};

// Close: the one write that ends joining and picking for a round. It also copies the roster as it
// stands at that instant into frozen_data, so the Durable callback (and any retry of it) carries
// exactly the picks that were accepted before the phase changed.
export const CLOSE = {
  expression: 'SET phase = :closing, lease_until = :lease, frozen_data = teams_data',
  condition: 'run_id = :id AND callback_id = :callback AND phase = :open AND round = :round',
};

// Retry of a Close whose callback AWS never confirmed: the roster was frozen by the first Close.
export const RETRY_CLOSE = {
  expression: 'SET lease_until = :lease',
  condition: 'run_id = :id AND callback_id = :callback AND phase = :closing AND round = :round AND lease_until <= :now',
};

export const RELEASE_CLOSE = {
  expression: 'REMOVE lease_until',
  condition: 'run_id = :id AND callback_id = :callback AND phase = :closing AND lease_until = :lease',
};

export function writeRoster(store, row, players) {
  return store.update(ROSTER_WRITE.expression, ROSTER_WRITE.condition, {
    ':next': s(JSON.stringify(players)), ':id': row.run_id, ':before': row.teams_data,
    ':ready': s('ready'), ':starting': s('starting'), ':open': s('open'), ':round': row.round ?? s(''),
  });
}

/** Moves open -> closing for this run, callback and round; returns the item after the move, or null. */
export function closeRound(store, row, round, lease) {
  return store.claim(CLOSE.expression, CLOSE.condition, {
    ':id': row.run_id, ':callback': row.callback_id, ':closing': s('closing'), ':lease': lease,
    ':open': s('open'), ':round': s(round),
  });
}

/** Takes the lease of a Close whose lease ran out; returns the item (with frozen_data) or null. */
export function retryClose(store, row, round, lease, now) {
  return store.claim(RETRY_CLOSE.expression, RETRY_CLOSE.condition, {
    ':id': row.run_id, ':callback': row.callback_id, ':closing': s('closing'), ':lease': lease,
    ':round': s(round), ':now': n(now),
  });
}

/** Hands the lease back once AWS has confirmed (or refused) the callback. */
export function releaseClose(store, row, lease) {
  return store.update(RELEASE_CLOSE.expression, RELEASE_CLOSE.condition, {
    ':id': row.run_id, ':callback': row.callback_id, ':closing': s('closing'), ':lease': lease,
  });
}
