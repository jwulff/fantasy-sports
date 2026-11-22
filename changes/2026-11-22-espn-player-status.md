# ESPN player status is not game-day availability

The normalized player status has always carried espn-api's activity label.
The dependency infers it from observed stat breakdowns across scoring periods;
even an empty prior-period breakdown can label a player `inactive` before the
current kickoff. The player-pool active flag is not official game-day clearance
either.

Document the existing field without changing v1 output or write safeguards.
An offline synthetic test shows that the label can change independently of a
player's injury designation and future kickoff.
