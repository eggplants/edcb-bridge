# Vendored KonomiTV EDCB client

`CtrlCmdUtil.py` and `__init__.py` are copied verbatim from
[tsukumijima/KonomiTV](https://github.com/tsukumijima/KonomiTV) `server/app/utils/edcb/`
(commit `07755942152b312a23ffb14b2877cd01ea48e62f`, MIT License, see `License.txt`).

The tests use them as the real EDCB client, so the bridge is exercised exactly as KonomiTV talks to it.
Set `KONOMITV_DIR` to a KonomiTV checkout to test against another version instead.
