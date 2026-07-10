from app.fetcher import looks_like_challenge

CHALLENGE = """
<html><head><title>Just a moment...</title></head>
<body><div id="challenge">Please complete the CAPTCHA to verify you are human.</div></body></html>
"""

# A genuine results page: an invisible reCAPTCHA iframe and the Cloudflare
# analytics beacon appear in the markup of USPhonebook, 411.com and ABC. Scanning
# raw HTML for "captcha"/"cloudflare" would flag all three as bot walls.
RESULTS_WITH_INVISIBLE_WIDGETS = """
<html><head><title>Jane Public in Massachusetts</title></head>
<body>
  <div class="card"><h2>Jane Public</h2><span>Age 36</span></div>
  <iframe src="https://www.google.com/recaptcha/api2/aframe" width="0" height="0" style="display: none"></iframe>
  <script defer src="https://static.cloudflareinsights.com/beacon.min.js"></script>
</body></html>
"""


def test_challenge_page_detected():
    assert looks_like_challenge(CHALLENGE)


def test_results_page_with_hidden_recaptcha_and_cf_beacon_is_not_a_challenge():
    assert not looks_like_challenge(RESULTS_WITH_INVISIBLE_WIDGETS)


def test_marker_in_visible_body_text_still_detected():
    assert looks_like_challenge("<html><body><p>Access denied.</p></body></html>")
