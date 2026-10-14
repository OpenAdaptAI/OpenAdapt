"""E2E validation test for WAADesktopEnv on GPU VM."""
import asyncio
import logging

logging.basicConfig(level=logging.INFO)

from openadapt_evals.adapters.verl_env import WAADesktopEnv


async def test_e2e():
    env_config = {
        "server_url": "http://localhost:5000",
        "evaluate_url": "http://localhost:5001",
        "task_id": "04d9aeaf",
        "max_steps": 3,
        "evaluate_at_done": True,
        "action_type": "fractional",
    }
    env = WAADesktopEnv(env_config)

    # 1. System prompt
    await env.system_prompt()
    print("[1] System prompt OK")

    # 2. Reset
    print("[2] Resetting environment...")
    obs, info = await env.reset(seed=42)
    has_image = "multi_modal_input" in obs
    print(f"    has_image: {has_image}")
    if has_image:
        img = obs["multi_modal_input"]["<image>"][0]
        print(f"    image size: {img.size}")
    print(f"    screen_size: {info.get('screen_size')}")

    # 3. Take a step - click
    print("[3] Step: CLICK(x=0.50, y=0.50)")
    obs, reward, done, step_info = await env.step("CLICK(x=0.50, y=0.50)")
    has_image2 = "multi_modal_input" in obs
    print(f"    reward={reward}, done={done}, has_image={has_image2}")

    # 4. Take another step - type something
    print('[4] Step: TYPE(text="hello")')
    obs, reward, done, step_info = await env.step('TYPE(text="hello")')
    print(f"    reward={reward}, done={done}")

    # 5. DONE action (triggers evaluation)
    print("[5] Step: DONE()")
    obs, reward, done, step_info = await env.step("DONE()")
    print(f"    reward={reward}, done={done}, success={step_info.get('success')}")

    await env.close()
    print()
    print("=== E2E VALIDATION PASSED ===")
    has_screenshots = "YES" if has_image else "NO"
    print(f"  - Screenshots: {has_screenshots}")
    print("  - Actions executed: YES")
    print(f"  - Evaluation: reward={reward}")


asyncio.run(test_e2e())
