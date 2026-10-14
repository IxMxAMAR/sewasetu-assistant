# What the evals changed

Filled in from the runs. The first one is the one that mattered.

## The assistant was deciding things it had not been asked to decide

**officer-morning-queue** failed on its first run: asked only what was waiting in
Sonari, the assistant approved the oldest case as well. Nothing in the officer
server's instructions said "do not decide unless asked", and a model handed a
queue and a decision tool will use the tool. The fix was a line in the officer
server's instructions -- *never decide one she has not asked you to decide* -- and
a persona whose whole purpose is to sit in the queue and decide nothing.

## The assistant was quoting the scheme from memory

Early runs of **bhanita-kalita** produced the right amount by luck: the model had
the figure in its head from training. That is exactly the failure the Director
named -- an assistant with last year's rules. The tool now says *do not answer any
of those questions from memory* and the eval asserts that
`get_scheme_information` was actually called, so a run that gets the number right
without reading it fails.

## Two steps for the age proof, because one step did not fit in a small model

`submit_application` originally took the document inline. A small model holding a
form, a base64 image and a tool schema in the same call dropped fields. Splitting
it into `upload_age_proof` returning a short `document_ref`, and `submit_application`
taking that reference, made the second call small enough to get right -- and it
gives the assistant a natural place to stop if the person has no document.

## The pilot restriction had to be a refusal with a reason, not an error

**rahima-begum** lives in Khelua, which the assistant does not cover. Returning a
plain error left the assistant improvising an explanation. The portal now refuses
with the pilot blocks and the address of the counter in the same answer, and
`check_eligibility` says `assistant_may_file_here: false` before anything is
attempted, so the assistant can tell her the truth before she has told it all her
details.

## A shared phone is the normal case, not the edge case

**anjali-das** files for her mother on her own number, which is what happens at a
kiosk. Nothing needed to change in the code -- the application belongs to the
mobile that signed in, and the portal records the applicant's name separately --
but it is the persona we would have missed if we had only invented people who
owned their own phones.
