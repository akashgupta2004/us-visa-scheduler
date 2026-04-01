import asyncio
from playwright.async_api import async_playwright

async def main():
    url = "https://atlasauth.b2clogin.com/f50ebcfb-eadd-41d8-9099-a7049d073f5c/b2c_1a_atoproduction_atlas_susi/oauth2/v2.0/authorize?client_id=607d08d6-b63b-4735-ad82-05dfcff7efa4&redirect_uri=https%3A%2F%2Fwww.usvisascheduling.com%2Fsignin-aad-b2c_1&response_type=code%20id_token&scope=openid&state=OpenIdConnect.AuthenticationProperties%3DCYcQP_vq8DCIXklk1FAYH96soyVD63dK7lWihizDd8rScXacJtlPZOLxeMNn9vEyY9ig3txKdoY3Cuco_xlgexy7tPfZbDKsH58DRlUtpoRMApw-zV6L1omCcIsTFU7r2qeziRl4EGZAAML0hU3HEeiG1AzSEB3nSFqIRmjwTcdr7Tn_dHwZO7tIJRbxrxRAL81MTq4lrjP6C10HyJcpeNSqK-LOlZ1JGoU0fkWlebXkrwz4EphaaWizBMkogJa7nkES-lOWeD5fUwFtgViHsnH0F5bPhwEgpI3tUjkHsZGRve0q_wOHSMZYtaKVrvV9LE0R7EvoDYLTmBmQXJ2tOH8uBhNzqfGUuv3dtlZnXIox6ytXzTx-HqUmdyVi0FiwSjlha-ROldG_XbssuGK_gLr5rv7Zfos0KTi0vaRgEeai8WII5ejRjPX7u9ttnBRzX_pWpJEwcptbcWCn8adTtMzdprA4YRxJXoSyWtNjcWEy2X6XXsr2EJEy2abGozMTwbQtELyhLwInAGWn4GyAk4LTWcXGmvsjMONJbR7y8ntPIwRVjDZZXcokRot5ucS6xWcpFK_eq7ljLz8jcRWKd_VFDLGpv9ybznGeoI2qmSKN30SssgmRivOiCuBNoOEC&response_mode=form_post&nonce=639106309734662214.ZGIwZmM4MjMtNGY4Yy00MzUzLTkwZjMtODQ2MDYyOTIyN2EzODUxY2MyZmEtY2ViNS00ZTQ4LWJhYmQtZjEzN2Y2ZTUxM2Zh&ui_locales=en-US&x-client-SKU=ID_NET472&x-client-ver=6.35.0.0"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False) # Keep it visible
        page = await browser.new_page()
        
        print("Navigating...")
        await page.goto(url)
        
        print("Waiting for captcha image...")
        img = page.locator('#captchaImage')
        await img.wait_for(state="visible", timeout=10000)
        
        print("Image is visible. Waiting 2 seconds for JS to populate src...")
        await page.wait_for_timeout(2000)
        
        src = await img.get_attribute("src")
        print(f"\n--- SRC ATTRIBUTE ---\n{src[:100]}...\n---------------------\n")
        
        if src and src.startswith("http"):
            print("It's a URL! Not a base64 string.")
        elif src and "base64" in src:
            print("It's a base64 string.")
        else:
            print("Unknown or empty src.")
            
        await browser.close()

asyncio.run(main())
