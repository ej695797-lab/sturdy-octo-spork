"""Find the Photon AppVersion the game uses: for each candidate, log into the
master server and read its AppStats event (players/games in that virtual app).
One connection at a time (each master connection holds a Photon CCU slot)."""
import asyncio, sys
from photon.client import Connection, NAME_SERVER, OP_AUTHENTICATE, auth_params, check, P_ADDRESS, P_TOKEN

APP, REGION = "00000000-0000-0000-0000-000000000003", "us"


async def stats_for(appver):
    ns = Connection(NAME_SERVER, APP, label="ns")
    await ns.open()
    try:
        p = check(await ns.op(OP_AUTHENTICATE, auth_params(APP, appver, REGION)), "ns auth")
    finally:
        await ns.close()
    got = asyncio.get_running_loop().create_future()

    def on_event(code, params):
        if code == 226 and not got.done():
            got.set_result(params)
    ms = Connection(p[P_ADDRESS], APP, on_event, label="master")
    await ms.open()
    try:
        check(await ms.op(OP_AUTHENTICATE, auth_params(APP, appver, REGION, token=p[P_TOKEN])), "master auth")
        st = await asyncio.wait_for(got, 8)
        return {"inGame": st.get(228), "onMaster": st.get(229), "games": st.get(227)}
    except asyncio.TimeoutError:
        return None
    finally:
        await ms.close()


async def main(cands):
    for c in cands:
        try:
            r = await stats_for(c)
        except Exception as e:
            r = f"error {e}"
        print(f"{c!r:28} -> {r}", flush=True)
        if isinstance(r, dict) and (r.get("inGame") or 0) > 1:
            print("FOUND", c); return


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
