ESPN activity topics already carry the moved player's ID. In the transaction
adapter, let espn-api use that ID instead of looking up players who are not
currently rostered. This removes unnecessary per-player requests without
dropping the activity feed or changing normalized trades, adds, drops, and bids.
