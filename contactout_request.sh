curl -X POST "https://api.contactout.com/v1/people/enrich" \
--header "Content-Type: application/json" \
--header "Accept: application/json" \
--header "token: o18jge1RIlq5NjMDLpAKV8i5" \
--data '{
  "full_name": "Jonathan Holman",
  "first_name": "Jonathan",
  "last_name": "Holman",
  "company": ["LONE STAR CONSTRUCTION CORP", "stripe"],
  "include": ["work_email", "personal_email", "phone"]
}'