# seed/

`seed.sql` is the Department of Social Welfare's own snapshot of the portal's
database — about 100,000 applications — and it is **not in this repository**.

It is fetched from the handout the department published, at first boot, by
`deploy/bootstrap.sh`. If you are running this locally, take `seed.sql` out of
the handout folder and put it here:

```
sewasetu-ai-handout/seed/seed.sql  ->  seed/seed.sql
```

Nothing else in the repository needs it; `docker compose up` and
`deploy/provision.sh` both skip the load when the `applications` table already
has rows in it.
