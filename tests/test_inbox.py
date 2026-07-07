from app.inbox import classify


def test_classify_confirmation():
    assert classify("Re: CCPA Request [PIR-3]",
                    "Your information has been deleted from our database.") == "confirmed"
    assert classify("Removal complete", "We have successfully removed your record.") == "confirmed"


def test_classify_verification_demand():
    assert classify("Action required [PIR-3]",
                    "Please verify your identity by uploading a photo of your ID.") == "needs_verification"
    assert classify("Confirm your request",
                    "Click the link below to confirm your removal request.") == "needs_verification"


def test_classify_rejection():
    assert classify("Re: your request",
                    "We were unable to locate any matching records for you.") == "rejected"
    assert classify("Request denied", "Your request was denied.") == "rejected"


def test_classify_unknown_goes_to_review():
    assert classify("Hello", "Thanks for contacting us, an agent will respond soon.") == "unknown"


def test_verification_beats_confirmation_when_ambiguous():
    # Contains both a "deleted" cue and a verification demand -> surface for review, safest.
    text = "Your data will be deleted once you verify your identity."
    assert classify("[PIR-9]", text) == "needs_verification"
