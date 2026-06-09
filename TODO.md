- NPC names taken from usecode not own list
- usecode offsets calculated not by fixed table for schedules
- single script build_all.py that imports the rest
- update readme
- [done] Emrichol, Tallon, Cardas, Daemos, Kothius, Mentar have no dialog?
  -> They do: thin-shell NPCs whose use() spawns the shared SORCERER class.
     parse_usecode.py now follows that spawn delegation (walk_delegated_dialog),
     gated on the target being a real conversation (I_ask / getName) so door
     mechanisms that bark "Locked Door" aren't misattributed.
