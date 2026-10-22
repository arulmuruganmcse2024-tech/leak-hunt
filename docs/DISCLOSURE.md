# Disclosure

The rules ask for original work, solely owned by the entrant, and for an explanation of any project that existed before the
submission period. This page says how the entry was made and what it claims.

## What this entry is

**Leak Hunt**: the hunt engine (`huntkit/`), the Alexa+ add-on (`addon/`: MCP server, MCP App screen, OAuth account linking,
pipe policy), the Pipe Hub specification and emulator, the tests, the verification tooling and the documents in this
repository. All of it was written during the submission period. None of it is a modified copy of an earlier project, and no
code from any earlier project is in this repository.

## How it was written

With AI coding assistance (Claude Code). The entrant is responsible for the contents, and every figure in `docs/` comes from a
command listed in [VERIFICATION.md](VERIFICATION.md) that anyone can re-run.

## The film

The demo film was made during the submission period. Its illustrations are original vector drawings made in code for the film;
its six screen inserts are recordings of this repository's dev host on the simulated house. **The narration and all voices
(narrator, Priya, Alexa) are AI-generated** with Kokoro-82M, an open-weight text-to-speech model (Apache-2.0, used through its
ONNX export), with espeak-ng (GPL-3.0) as the pronunciation step; neither ships in the repository. The Alexa voice is not
Amazon's. The music bed and sound effects are synthesised from sine waves and noise for the film. No third-party music, samples,
footage or logos are used.

## Existing work

None. Leak Hunt was built during the submission period, and the entry depends on no earlier project. The demo, the
304 tests, the verification runs and the film all run on the **simulated house** (`addon/sources.py`), with no sensor and no
hardware. Nothing in the user-facing text names a product: the screen and the spoken lines say "your water monitor" and "your
main safety valve".

## What is claimed as new, and what is not claimed

* New in the submission period: everything listed under "What this entry is".
* Not claimed: any measurement with real water. No real water, sensor or valve is involved in any number in this
  repository. See [HONEST_UNKNOWNS.md](HONEST_UNKNOWNS.md).
* **Pipe Hub** (the optional controller that lets Alexa switch individual pipes) is a *specification* written during the
  hackathon, with a software emulator (`tools/pipehub_emulator.py`) and a REST client (`addon/actuators.py`). **No firmware,
  board or valve exists.** No device's main safety valve is addressed by this add-on.
* The add-on can switch pipes only through a Pipe Hub that the operator configures. Until someone builds and tests one,
  everything that moves a "pipe" in this repository is a simulation and says so on screen.
