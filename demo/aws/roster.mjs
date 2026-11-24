// The two conditional writes that decide who is in the round. `store` offers
//   update(expression, condition, values)  -> boolean
//   claim(expression, condition, values)   -> the item after the write (ALL_NEW), or null
// so the ordering rules can be tested without DynamoDB.
export const s = value => ({ S: value });
export const n = value => ({ N: String(value) });

export const ROSTER_WRITE = {
  expression: 'SET teams_data = :next',
  condition: 'run_id = :id AND phase IN (:ready, :launching, :launch) AND teams_data = :before',
};

// Close: the one write that ends joining and picking. It also copies the roster as it
// stands at that instant into frozen_data, so the Durable callback (and any retry of
// it) carries exactly the picks that were accepted before the phase changed.
export const CLOSE = {
  expression: 'SET phase = :busy, lease_until = :lease, frozen_data = teams_data',
  condition: 'run_id = :id AND callback_id = :callback AND phase = :waiting',
};

export function writeRoster(store, row, teams) {
  return store.update(ROSTER_WRITE.expression, ROSTER_WRITE.condition, {
    ':next': s(JSON.stringify(teams)), ':id': row.run_id, ':before': row.teams_data,
    ':ready': s('ready'), ':launching': s('launching'), ':launch': s('launch'),
  });
}

/** Moves launch -> migrating for this run and callback; returns the item after the move, or null. */
export function closeRound(store, row, lease) {
  return store.claim(CLOSE.expression, CLOSE.condition, {
    ':id': row.run_id, ':callback': row.callback_id, ':busy': s('migrating'), ':lease': lease, ':waiting': s('launch'),
  });
}
