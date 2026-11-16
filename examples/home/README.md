# Example home folder

A complete home folder for a made-up applicant, Alex Doe. Nothing here
describes a real person. Tests, documentation, and screenshots use it.

Try the tool against it:

```
uv run jobs profile check --home examples/home
```

It holds `config.yaml` (sources, targets, filters) and `profile/` (the truth
profile). It has no `.env` file. Your own home folder also holds a `.env`
file with your Claude API key (see `.env.example` in the repository) and a
`data/` folder that the app creates.

To start your own, copy this folder to somewhere **outside** the repository,
point `JOBTOOL_HOME` at it, and replace the contents with your own facts.
