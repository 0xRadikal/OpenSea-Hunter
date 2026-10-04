"""Run aerent_mint.py UNCHANGED against the live test drop by overriding only target constants.
Every other line of the production code path (API, validation, gas, broadcast to sequencer) is real.
usage: run_testdrop.py T0 LABEL T_END TARGET_HELD"""
import sys, importlib.util
sp = importlib.util.spec_from_file_location("bot", __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "aerent_mint.py"))
bot = importlib.util.module_from_spec(sp); sp.loader.exec_module(bot)
bot.SLUG = "bottest-464316076"
bot.NFT = "0x728924f13327f17b5be06b00fd26a6df2a2aacb6"
bot.STAGE_START = int(sys.argv[1])
bot.STAGE_LABEL = sys.argv[2]
bot.STAGE_END = int(sys.argv[3])
bot.TARGET_HELD = int(sys.argv[4])
sys.argv = ["aerent_mint.py", "--arm"]
bot.main()
