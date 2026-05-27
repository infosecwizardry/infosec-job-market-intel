from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from job_market_intel.collectors import CollectorResult
from job_market_intel.dedup import dedup_listings, listing_hash
from job_market_intel.extract import regex_rules
from job_market_intel.extract.llm import ClaudeExtractor, _parse_json
from job_market_intel.models import ExtractedRequirements, Listing
from job_market_intel.pipeline import Pipeline, PipelineOptions
from job_market_intel.reporting import render_markdown_report
from job_market_intel.scoring import tabulate
from job_market_intel.seeds import (
    classify_role,
    classify_seniority,
    classify_seniority_combined,
    classify_seniority_from_description,
    is_bureaucratic_metadata_only,
    is_compliance_role,
    is_physical_security_role,
    is_us_location,
)

WORKSPACE_TEMP_ROOT = Path.cwd() / ".tmp-test-work"
WORKSPACE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


class WorkspaceTempDir:
    def __enter__(self) -> str:
        self.path = WORKSPACE_TEMP_ROOT / f"case-{uuid4().hex}"
        self.path.mkdir(parents=True, exist_ok=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def make_listing(
    *,
    title: str,
    company: str,
    location: str,
    description: str,
    source: str = "indeed",
    url: str = "https://example.test/job/1",
) -> Listing:
    return Listing(
        listing_id=listing_hash(company, title, location),
        title=title,
        company=company,
        location=location,
        description=description,
        role_bucket=classify_role(title),
        sources=[source],
        source_urls=[url],
        posted_at="2026-05-20",
        fetched_at="2026-05-24T00:00:00Z",
    )


class FakeCollector:
    """Returns the same canned list every call, with a per-call queries log."""

    def __init__(self, *, source_name: str, listings: list[Listing], warnings: list[str] | None = None) -> None:
        self.source_name = source_name
        self._listings = listings
        self._warnings = warnings or []
        self.calls: list[tuple[tuple[str, ...], str, int, int]] = []

    def collect(
        self,
        *,
        queries: list[str],
        location: str,
        results_per_query: int,
        freshness_days: int = 14,
    ) -> CollectorResult:
        self.calls.append((tuple(queries), location, results_per_query, freshness_days))
        return CollectorResult(
            listings=list(self._listings),
            warnings=list(self._warnings),
            source_name=self.source_name,
        )


class FakeClaudeExtractor(ClaudeExtractor):
    """ClaudeExtractor that never calls the network — returns canned payload."""

    def __init__(self, *, cache_dir: Path, payload: dict | None = None) -> None:
        super().__init__(api_key="test-key", cache_dir=cache_dir)
        self.payload = payload or {
            "responsibilities": ["Triage SIEM alerts", "Escalate confirmed incidents"],
            "technical_skills": ["Splunk", "PowerShell", "MITRE ATT&CK"],
            "seniority_signal": "entry",
            "remote_arrangement": "remote",
        }
        self.calls: list[str] = []

    def _call_claude(self, *, description: str) -> dict:  # type: ignore[override]
        self.calls.append(description)
        return self.payload


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class ClassificationTests(TestCase):
    def test_soc_titles_bucket_correctly(self) -> None:
        self.assertEqual(classify_role("Junior SOC Analyst"), "junior_soc")
        self.assertEqual(classify_role("Security Operations Center Analyst I"), "junior_soc")
        self.assertEqual(classify_role("Tier 1 SOC Analyst"), "junior_soc")

    def test_help_desk_titles_bucket_correctly(self) -> None:
        self.assertEqual(classify_role("Help Desk Technician"), "help_desk_it_admin")
        self.assertEqual(classify_role("IT Support Specialist"), "help_desk_it_admin")
        self.assertEqual(classify_role("Systems Administrator"), "help_desk_it_admin")
        self.assertEqual(classify_role("Sysadmin (Linux)"), "help_desk_it_admin")

    def test_unrelated_titles_are_unclassified(self) -> None:
        self.assertEqual(classify_role("Senior Backend Engineer"), "unclassified")
        self.assertEqual(classify_role("Product Manager"), "unclassified")


class ClassifySeniorityTests(TestCase):
    def test_leadership_keywords(self) -> None:
        for title in [
            "Director of Security Operations",
            "VP, Information Security",
            "Manager, IT Support",
            "Head of Cybersecurity",
            "Chief Information Security Officer",
            "Security Architect",
        ]:
            self.assertEqual(classify_seniority(title), "leadership", title)

    def test_senior_keywords(self) -> None:
        for title in [
            "Senior SOC Analyst",
            "Sr. Security Engineer",
            "Principal SOC Analyst",
            "Staff Security Engineer",
            "Lead Cybersecurity Analyst",
            "Cybersecurity Analyst III",
            "SOC Analyst Tier 3",
        ]:
            self.assertEqual(classify_seniority(title), "senior", title)

    def test_mid_keywords(self) -> None:
        for title in [
            "SOC Analyst II",
            "Cybersecurity Analyst II",
            "Tier 2 SOC Analyst",
            "Mid-Level Security Engineer",
            "Intermediate IT Support",
        ]:
            self.assertEqual(classify_seniority(title), "mid", title)

    def test_entry_keywords(self) -> None:
        for title in [
            "Junior SOC Analyst",
            "Jr. Security Operations Analyst",
            "Entry Level Cybersecurity Analyst",
            "Associate IT Support Specialist",
            "SOC Analyst I",
            "SOC Analyst Tier 1",
            "Cybersecurity Analyst Intern",
            "IT Support Trainee",
            "New Grad Security Analyst",
        ]:
            self.assertEqual(classify_seniority(title), "entry", title)

    def test_unclear_keywords(self) -> None:
        # Bare titles with no level modifier.
        for title in [
            "SOC Analyst",
            "Cybersecurity Analyst",
            "Help Desk Technician",
            "IT Support Specialist",
            "Systems Administrator",
        ]:
            self.assertEqual(classify_seniority(title), "unclear", title)

    def test_priority_leadership_beats_senior(self) -> None:
        # "Senior IT Manager" → leadership, not senior.
        self.assertEqual(classify_seniority("Senior IT Manager"), "leadership")

    def test_priority_senior_beats_entry(self) -> None:
        # "Senior Associate" → senior (the "Senior" modifier wins).
        self.assertEqual(classify_seniority("Senior Associate Engineer"), "senior")

    def test_empty_or_none_is_unclear(self) -> None:
        self.assertEqual(classify_seniority(""), "unclear")
        self.assertEqual(classify_seniority(None), "unclear")  # type: ignore[arg-type]

    def test_ii_does_not_match_iii(self) -> None:
        # Regression: "III" must not match the mid-level "II" pattern.
        self.assertEqual(classify_seniority("Cybersecurity Analyst III"), "senior")


class ClassifySeniorityFromDescriptionTests(TestCase):
    def test_senior_keywords_win(self) -> None:
        for body in [
            "Position requires 5+ years of cybersecurity experience.",
            "Minimum of 7 years in security operations is required.",
            "You will lead a team of SOC analysts.",
            "Mentor junior analysts on detection workflows.",
            "Looking for a subject-matter expert in incident response.",
            "Senior-level role with deep expertise in SIEM tuning.",
            "10+ years of experience required.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_entry_keywords_win(self) -> None:
        for body in [
            "Entry-level position for recent graduates.",
            "No prior experience required — training will be provided.",
            "Recent graduate or 0-2 years of IT experience welcome.",
            "Looking for early-career candidates.",
            "Your first cybersecurity role — we will train you.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "entry", body)

    def test_senior_beats_entry_when_both_present(self) -> None:
        # If a description mentions both, senior takes priority (conservative).
        body = "Entry-level candidates considered, but 5+ years of experience preferred."
        self.assertEqual(classify_seniority_from_description(body), "senior")

    def test_yoe_bucket_fallback_when_no_keyword_match(self) -> None:
        # No keyword hits — fall back to YoE bucketing.
        self.assertEqual(classify_seniority_from_description("(no signals)", yoe_min=2), "entry")
        self.assertEqual(classify_seniority_from_description("(no signals)", yoe_min=4), "mid")
        self.assertEqual(classify_seniority_from_description("(no signals)", yoe_min=8), "senior")

    def test_no_signal_returns_unclear(self) -> None:
        self.assertEqual(classify_seniority_from_description("Generic description.", yoe_min=None), "unclear")
        self.assertEqual(classify_seniority_from_description("", yoe_min=None), "unclear")
        self.assertEqual(classify_seniority_from_description(None, yoe_min=None), "unclear")

    def test_widened_senior_signals_in_body(self) -> None:
        # Round 4 additions: phrasings the previous regex missed.
        for body in [
            "Looking for someone with 7 years of background in security operations.",
            "Position is at Tier 3 within the SOC.",
            "Mentor team members and coach junior analysts on detection workflows.",
            "Principal engineer responsible for detection architecture.",
            "Supervisory responsibilities for the night shift.",
            "Set the strategy for our threat hunting program.",
            "8 years or more of cybersecurity required.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_yoe_veto_blocks_entry_classification(self) -> None:
        """A listing asking for 3+ years cannot be entry-level no matter what
        the title says. This catches roles like 'Junior Specialist' / 'IT
        Analyst' that pair an entry-flavored title with a 3-5 or 6+ year bar."""
        # Title would say "entry" (Junior), but YoE=4 vetoes it → mid.
        self.assertEqual(
            classify_seniority_combined("Junior IT Analyst", "5+ years of experience.", yoe_min=4),
            "mid",
        )
        # Title would say "entry" via "I" suffix, YoE=6 vetoes it → senior.
        self.assertEqual(
            classify_seniority_combined("Specialist I", "6+ years required.", yoe_min=6),
            "senior",
        )
        # Bare title (unclear), YoE=3 → mid (no entry classification anywhere).
        self.assertEqual(
            classify_seniority_combined("IT Support Technician", "3-5 years required.", yoe_min=3),
            "mid",
        )

    def test_yoe_veto_does_not_demote_senior_titles(self) -> None:
        """Senior/leadership titles still win even when YoE is only 3 — a
        company calling someone 'Senior Engineer' with 3 years of YoE listed
        is still a senior posting (the title is the explicit signal)."""
        self.assertEqual(
            classify_seniority_combined("Senior SOC Analyst", "3+ years required.", yoe_min=3),
            "senior",
        )
        self.assertEqual(
            classify_seniority_combined("Director of IT", "minimum 4 years.", yoe_min=4),
            "leadership",
        )

    def test_yoe_below_threshold_keeps_entry(self) -> None:
        """YoE of 0-2 doesn't trigger the veto."""
        self.assertEqual(
            classify_seniority_combined("Junior IT Analyst", "0-2 years.", yoe_min=2),
            "entry",
        )
        self.assertEqual(
            classify_seniority_combined("Junior IT Analyst", "1+ years.", yoe_min=1),
            "entry",
        )

    def test_round7_strategic_and_sole_contributor_senior_signals(self) -> None:
        # Round 7: phrases the LLM cited as leadership clues. Anchored on
        # role-scope language so an entry listing that mentions "VP" or
        # "strategy" in passing doesn't get falsely flagged.
        for body in [
            "You will drive the strategic direction of our security program.",
            "Owns the strategic direction across detection engineering.",
            "Sole contributor role responsible for the entire IAM stack.",
            "This is a sole-contributor position reporting directly to the VP.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_widened_entry_signals_in_body(self) -> None:
        for body in [
            "Junior SOC analyst position in our 24x7 SOC.",
            "This role is the first SOC role for a new graduate.",
            "We accept fresh graduates with no security experience.",
            "Apprenticeship program — no prior experience required.",
            "Tier 1 SOC role — first cybersecurity role for the right person.",
            "We offer on-the-job training to motivated candidates.",
            "Looking for a junior engineer.",
            "0+ years of experience required.",
            "Graduate program for entry-level cybersecurity professionals.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "entry", body)

    def test_yoe_body_scan_finds_standalone_n_years(self) -> None:
        # "7 years" with no preceding "experience" word in the same sentence;
        # the strict cue ("of") sits within the 60-char window.
        body = "We seek someone with 7 years of cybersecurity background."
        self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_yoe_body_scan_ignores_unrelated_year_mentions(self) -> None:
        # "founded 30 years ago" shouldn't be read as a YoE requirement.
        body = "Our company was founded 30 years ago and now operates globally."
        # No senior/entry keywords, no cue-word context for the 30 → unclear.
        self.assertEqual(classify_seniority_from_description(body), "unclear")

    def test_extract_yoe_from_description_smallest_value_wins(self) -> None:
        from job_market_intel.seeds import _extract_yoe_from_description

        body = (
            "Position requires a minimum of 2 years of relevant experience. "
            "Candidates with 5 years of background are preferred."
        )
        # Smallest valid value wins (the stated minimum).
        self.assertEqual(_extract_yoe_from_description(body), 2)

    def test_extract_yoe_from_description_returns_none_without_cues(self) -> None:
        from job_market_intel.seeds import _extract_yoe_from_description

        # No cue words anywhere within 60 chars of the number.
        body = "Established in 2010. Our headquarters has been there for 5 years."
        self.assertIsNone(_extract_yoe_from_description(body))

    # Round 5 — wider description patterns
    def test_round5_entry_signals(self) -> None:
        for body in [
            "Working knowledge of Active Directory and Group Policy is required.",
            "Basic knowledge of networking concepts (TCP/IP, DNS, DHCP).",
            "Basic understanding of cybersecurity principles required.",
            "Basic familiarity with SIEM tools is a plus.",
            "Requires 6+ months of experience in technical support.",
            "12 months of experience troubleshooting Windows.",
            "Up to 2 years of relevant experience preferred.",
            "Up to one year of IT experience.",
            "One to two years of help desk experience.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "entry", body)

    def test_round5_senior_signals(self) -> None:
        for body in [
            "Proven experience leading SOC operations.",
            "Proven expertise in incident response engineering.",
            "Proven track record of mentoring junior engineers.",
            "Demonstrated experience scaling detection programs.",
            "Demonstrated leadership across multiple teams.",
            "Demonstrated ability to lead cross-functional initiatives.",
            "Over 7 years of cybersecurity experience.",
            "Over 15 years in security architecture.",
            "Deep knowledge of SIEM tuning required.",
            "Advanced understanding of threat hunting methodology required.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_round5_avoids_false_positive_company_history(self) -> None:
        # "For over 20 years, we've set the standard" — company history, NOT a
        # YoE requirement. Must not classify as senior.
        for body in [
            "For over 20 years, we've set the standard for IT services.",
            "Our company has been operating for over 15 years.",
            "Established over 10 years ago in the cloud security space.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "unclear", body)

    def test_round5_avoids_false_positive_marketing_knowledge(self) -> None:
        # "deep knowledge of our customers' needs / our missions / our products" —
        # marketing copy about the company, NOT a candidate requirement. Must not
        # classify as senior.
        for body in [
            "We apply our proven solutions to a deep knowledge of Defense and Civilian missions.",
            "Our team has deep knowledge of our customers' needs.",
            "Advanced knowledge of our products and services.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "unclear", body)

    def test_round5_keeps_true_positive_deep_knowledge_tech_domain(self) -> None:
        # When followed by a technical domain noun, deep/advanced knowledge IS a
        # legitimate senior signal.
        for body in [
            "Deep understanding of TCP/IP, DNS, HTTP/S, and packet-level analysis.",
            "Advanced knowledge of networking and firewalls required.",
            "Deep knowledge of incident response procedures.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)

    def test_round5_keeps_true_positive_over_n_years_of_experience(self) -> None:
        for body in [
            "Over 7 years of cybersecurity experience required.",
            "Over 10 years of professional experience in security operations.",
            "Over 8 years in the field of cyber defense.",
        ]:
            self.assertEqual(classify_seniority_from_description(body), "senior", body)


class ClassifySeniorityCombinedTests(TestCase):
    def test_explicit_title_wins_when_yoe_is_low(self) -> None:
        # Title says Junior, no high YoE bar → title wins (entry).
        result = classify_seniority_combined(
            "Junior SOC Analyst",
            description="Some description with no senior signal.",
            yoe_min=1,
        )
        self.assertEqual(result, "entry")

    def test_yoe_veto_overrides_junior_title_when_yoe_high(self) -> None:
        # Title says Junior, BUT the listing actually requires 5 years. Per
        # the YoE-veto rule, this is not entry-level even though the title
        # is "Junior" — the experience bar reflects the real seniority.
        result = classify_seniority_combined(
            "Junior SOC Analyst",
            description="Looking for 5+ years of SOC experience.",
            yoe_min=5,
        )
        self.assertEqual(result, "mid")

    def test_unclear_title_falls_back_to_description_senior(self) -> None:
        result = classify_seniority_combined(
            "Cyber Security Analyst",
            description="Looking for someone with 7+ years of SOC experience.",
            yoe_min=7,
        )
        self.assertEqual(result, "senior")

    def test_unclear_title_falls_back_to_description_entry(self) -> None:
        result = classify_seniority_combined(
            "SOC Analyst",
            description="Recent graduates encouraged to apply — no prior experience required.",
            yoe_min=None,
        )
        self.assertEqual(result, "entry")

    def test_unclear_title_falls_back_to_yoe_bucket(self) -> None:
        result = classify_seniority_combined(
            "Information Security Analyst",
            description="The role requires technical aptitude.",  # no keyword
            yoe_min=4,
        )
        self.assertEqual(result, "mid")

    def test_unclear_title_no_description_signal_stays_unclear(self) -> None:
        result = classify_seniority_combined("SOC Analyst", description="(no signals)", yoe_min=None)
        self.assertEqual(result, "unclear")


class IsBureaucraticMetadataOnlyTests(TestCase):
    def test_guilford_county_style_returns_true(self) -> None:
        # The actual Guilford County Schools posting that prompted this filter.
        body = (
            "JobID: 43386    Position Type: Classified - Information Technology    "
            "Date Posted: 5/19/2026    Location: TECHNOLOGY SERVICES    "
            "Date Available: 05/19/2026    Fair Labor Standards Act Classification: Exempt    "
            "Position Term: 12 month    Classification: Continuing    "
            "Time Basis: Full-Time    Benefits: Full    "
            "Starting Salary: $4,255.00 per month    Pay Grade: 75 12 Month/Salary Schedule    "
            "Master Salary Schedule"
        )
        self.assertTrue(is_bureaucratic_metadata_only(body))

    def test_real_posting_with_duties_returns_false(self) -> None:
        # A real gov posting with metadata AND actual job content stays.
        body = (
            "Position Type: Classified - Information Technology. FLSA: Exempt. "
            "Pay Grade: 75. The Systems Administrator is responsible for managing "
            "Active Directory, troubleshooting end-user issues, and maintaining "
            "Windows Server environments. Requirements: 2+ years of experience."
        )
        self.assertFalse(is_bureaucratic_metadata_only(body))

    def test_long_description_returns_false_regardless(self) -> None:
        # Length threshold guards against catching anything substantive.
        body = "FLSA Exempt. Pay Grade 5. Position Type: Classified. " * 50
        # Now length is ~3000 chars — too long to be the "metadata-only" pattern.
        self.assertFalse(is_bureaucratic_metadata_only(body))

    def test_empty_or_none_returns_false(self) -> None:
        self.assertFalse(is_bureaucratic_metadata_only(""))
        self.assertFalse(is_bureaucratic_metadata_only(None))

    def test_short_without_metadata_returns_false(self) -> None:
        # Short generic description without metadata fields shouldn't match.
        body = "Looking for an IT support specialist to join our team. Apply now."
        self.assertFalse(is_bureaucratic_metadata_only(body))


class IsComplianceRoleTests(TestCase):
    def test_clearly_grc_role_is_flagged(self) -> None:
        body = (
            "The Information Security Analyst will lead our GRC program, "
            "performing internal audits and SOX compliance assessments, "
            "managing the compliance framework, and coordinating with our "
            "regulatory examination team. You will own our ISO 27001 program, "
            "perform control testing across our vendor risk management and "
            "third-party risk register. Hands-on with NIST 800-53 controls "
            "and policy development is required."
        )
        self.assertTrue(is_compliance_role(body))

    def test_real_soc_role_with_compliance_mention_is_not_flagged(self) -> None:
        """A SOC role that mentions compliance in passing should NOT be flagged."""
        body = (
            "Join our 24x7 SOC as a Tier 1 analyst. You will perform alert "
            "triage in Splunk and CrowdStrike, conduct incident response, "
            "and assist with threat hunting. Some exposure to SOX compliance "
            "reporting is a plus but not required. Detection engineering "
            "experience welcome. EDR and log analysis are daily activities."
        )
        # Has operational SOC terms — must be excluded from compliance bucket.
        self.assertFalse(is_compliance_role(body))

    def test_short_or_empty_description_not_flagged(self) -> None:
        self.assertFalse(is_compliance_role(None))
        self.assertFalse(is_compliance_role(""))
        self.assertFalse(is_compliance_role("Short blurb."))

    def test_single_compliance_mention_not_enough(self) -> None:
        """One compliance term alone shouldn't trigger — needs at least 3."""
        body = (
            "We are seeking an Information Security Analyst to join our team. "
            "You will help with audit findings remediation periodically. "
            "Our team works on security projects and helps protect the business. "
            "We value people who are curious and self-directed. " * 2
        )
        # Only "audit findings" (1 term) and maybe nothing else compliance-specific.
        self.assertFalse(is_compliance_role(body))

    def test_dod_rmf_stig_role_is_flagged(self) -> None:
        """RMF/STIG/system-accreditation work is GRC, not SOC monitoring.

        Real example: 'Junior Cybersecurity Analyst (RMF / Vulnerability
        Management / Cloud Security)' at SS3G. Description focuses on DoD
        RMF, STIG hardening, and Security Impact Analysis — distinct job
        family from alert triage / IR.
        """
        body = (
            "SS3G is seeking a motivated Junior Cybersecurity Analyst to support "
            "cybersecurity compliance, vulnerability management, and system "
            "security oversight activities within classified DoD environments. "
            "Validate system configurations to ensure required STIGs, hardening "
            "requirements, and cybersecurity controls are implemented in "
            "accordance with DoD RMF and the JSIG. Conduct vulnerability scans "
            "and track Security Impact Assessment items. Configuration "
            "validation is a daily activity. Authority to operate documentation "
            "is also part of the role."
        )
        self.assertTrue(is_compliance_role(body))


class IsPhysicalSecurityRoleTests(TestCase):
    def test_physical_security_terms_no_cyber_returns_true(self) -> None:
        for body in [
            # Oregon Capitol style
            "Capitol Safety and Security team responsible for daily building security operations through "
            "risk management, physical screening, electronic access control systems, video monitoring, "
            "and fixed security posts.",
            # Metro One LPSG style
            "Centralized hub for physical security operations, responsible for real-time alarm monitoring, "
            "incident triage, and coordinated response. Monitor and analyze alarms, access control, and "
            "intrusion systems to identify true threats.",
            # Mall / corporate facility
            "Security officer position. Roving patrol of campus security. CCTV and badging operations.",
        ]:
            self.assertTrue(is_physical_security_role(body), body[:60])

    def test_cyber_terms_present_returns_false(self) -> None:
        # Real cyber SOC posting — even with physical-adjacent words like "alarm".
        for body in [
            "SOC Analyst monitoring Splunk SIEM alerts. Triage incidents, escalate to Tier 2. "
            "Investigate malware infections and phishing campaigns.",
            "Tier 1 SOC analyst. Tools: QRadar, CrowdStrike, MITRE ATT&CK framework. Active Directory.",
            "We monitor alarms generated by our endpoint detection (EDR) platform.",
        ]:
            self.assertFalse(is_physical_security_role(body), body[:60])

    def test_metro_one_intrusion_detection_is_still_physical(self) -> None:
        """Metro One LPSG / M1 Global style: the term 'intrusion detection
        systems' overlaps cyber + physical vocabularies. With 3 strong
        physical terms and only 1 borderline cyber term, the listing should
        still classify as physical-security (strict-majority rule)."""
        body = (
            "M1 Global is looking for a driven SOC Analyst. In this role, "
            "you'll be at the center of physical security operations — "
            "monitoring alarms, coordinating response, supporting crisis "
            "management. Monitor and assess alarms, access control systems, "
            "intrusion detection systems, and video surveillance to identify "
            "threats. CCTV monitoring across multiple sites is the daily core. "
            "Site security and building security audits also part of the role."
        )
        self.assertTrue(is_physical_security_role(body))

    def test_single_physical_term_is_not_enough(self) -> None:
        """A real cyber listing that happens to mention 'badging' once
        should NOT be reclassified as physical."""
        body = (
            "Tier 1 SOC Analyst. You'll work with Splunk SIEM, CrowdStrike "
            "EDR, MITRE ATT&CK detections, threat intelligence, malware "
            "analysis. Visitor badging access is provided on day one."
        )
        self.assertFalse(is_physical_security_role(body))

    def test_decisive_physical_term_overrides_majority(self) -> None:
        """Decisive terms (CCTV, security guard, roving patrol, metal detector...)
        flip the call to physical even if cyber terms outnumber other physical
        signals. These words literally never appear in a cyber-SOC posting."""
        # CCTV alone is enough — even paired with multiple cyber-sounding terms.
        body_cctv = (
            "Security analyst role. You will monitor SIEM alerts and review "
            "CCTV footage during incident response. Active Directory "
            "experience required. SOAR experience a plus."
        )
        self.assertTrue(is_physical_security_role(body_cctv))

        # 'security guard' is decisive — even if the description tries to
        # sound cyber-flavored.
        body_guard = (
            "Security guard rotation includes assisting with cybersecurity "
            "training, ransomware awareness sessions, and active directory "
            "lockout requests at the front desk."
        )
        self.assertTrue(is_physical_security_role(body_guard))

        # Roving patrol is decisive.
        body_patrol = (
            "Roving patrol of the data center facilities. Some interaction "
            "with our SOC team and SIEM dashboard required during incidents."
        )
        self.assertTrue(is_physical_security_role(body_patrol))

    def test_empty_description_returns_false(self) -> None:
        # No body = no judgment. Don't reclassify on absence of signal.
        self.assertFalse(is_physical_security_role(""))
        self.assertFalse(is_physical_security_role(None))

    def test_neither_signal_returns_false(self) -> None:
        # Generic description with neither cyber nor physical terms — keep as-is.
        body = "This is a great opportunity to join our team. Apply today!"
        self.assertFalse(is_physical_security_role(body))


class IsUsLocationTests(TestCase):
    def test_us_state_code_returns_true(self) -> None:
        for loc in ["Austin, TX", "San Francisco, CA", "Tampa, FL", "New York, NY"]:
            self.assertTrue(is_us_location(loc), loc)

    def test_us_positive_marker_returns_true(self) -> None:
        for loc in ["United States", "Remote - US", "Remote (US)", "USA", "US-Remote"]:
            self.assertTrue(is_us_location(loc), loc)

    def test_empty_or_none_returns_true(self) -> None:
        self.assertTrue(is_us_location(""))
        self.assertTrue(is_us_location(None))

    def test_bare_remote_returns_true(self) -> None:
        # Ambiguous "Remote" with no geography should default to include.
        self.assertTrue(is_us_location("Remote"))

    def test_clear_non_us_returns_false(self) -> None:
        for loc in [
            "London, UK",
            "Berlin, Germany",
            "Bangalore, India",
            "Toronto, Canada",
            "Sydney, Australia",
            "Tokyo, Japan",
            "Remote - EMEA",
            "Dublin, Ireland",
            "Singapore",
        ]:
            self.assertFalse(is_us_location(loc), loc)

    def test_multi_location_with_us_state_wins(self) -> None:
        # When a US state code is present, multi-location strings still count as US.
        self.assertTrue(is_us_location("New York, NY / London"))
        self.assertTrue(is_us_location("Remote — Austin TX or Berlin"))

    def test_lowercase_uk_token_does_not_falsely_match(self) -> None:
        # Word boundaries: "ukraine" shouldn't trigger via " uk," matcher.
        # Confirm Ukraine is rejected correctly.
        self.assertFalse(is_us_location("Kyiv, Ukraine"))

    def test_random_two_letter_word_does_not_match_state(self) -> None:
        # Should not treat "NO" in "Oslo, NO" as the US state NO (no such state),
        # nor "BE" as the state BE — these aren't real US state codes.
        self.assertFalse(is_us_location("Oslo, Norway"))
        self.assertFalse(is_us_location("Brussels, Belgium"))


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class DedupTests(TestCase):
    def test_same_company_title_location_collapses_and_unions_sources(self) -> None:
        a = make_listing(
            title="Junior SOC Analyst",
            company="Acme Corp.",
            location="Remote",
            description="short",
            source="indeed",
            url="https://indeed.test/1",
        )
        b = make_listing(
            title="Junior SOC Analyst",
            company="acme corp",  # punctuation/case variant
            location="REMOTE",
            description="a much longer description that should win on length",
            source="linkedin",
            url="https://linkedin.test/1",
        )
        merged = dedup_listings([a, b])
        self.assertEqual(len(merged), 1)
        out = merged[0]
        self.assertEqual(sorted(out.sources), ["indeed", "linkedin"])
        self.assertIn("https://indeed.test/1", out.source_urls)
        self.assertIn("https://linkedin.test/1", out.source_urls)
        # Longest description wins.
        self.assertIn("longer description", out.description)

    def test_distinct_titles_or_companies_remain_separate(self) -> None:
        a = make_listing(title="Junior SOC Analyst", company="A", location="Remote", description="x")
        b = make_listing(title="Senior SOC Analyst", company="A", location="Remote", description="x")
        c = make_listing(title="Junior SOC Analyst", company="B", location="Remote", description="x")
        merged = dedup_listings([a, b, c])
        self.assertEqual(len(merged), 3)


# ---------------------------------------------------------------------------
# Regex extraction — the 20-ish real-world phrasings called out in the plan
# ---------------------------------------------------------------------------


class RegexExtractionTests(TestCase):
    def test_cert_dictionary_matches_canonical_variants(self) -> None:
        text = (
            "Required: CompTIA Security+. Preferred: Network+, CySA+, CCNA, CISSP, OSCP. "
            "Familiarity with ITIL and SC-200 a plus. AWS Certified Cloud Practitioner desired."
        )
        result = regex_rules.extract(text)
        for cert in [
            "Security+",
            "Network+",
            "CySA+",
            "CCNA",
            "CISSP",
            "OSCP",
            "ITIL",
            "SC-200",
            "AWS Certified Cloud Practitioner",
        ]:
            self.assertIn(cert, result.certifications, f"missing {cert}")

    def test_sec_plus_shorthand_is_recognized(self) -> None:
        result = regex_rules.extract("Sec+ required, A+ a plus.")
        self.assertIn("Security+", result.certifications)
        self.assertIn("A+", result.certifications)

    def test_yoe_takes_minimum_when_multiple_present(self) -> None:
        text = (
            "2+ years of experience in IT support required. " "5 years of experience in security operations preferred."
        )
        lo, hi = regex_rules.extract(text).years_experience_min, regex_rules.extract(text).years_experience_max
        self.assertEqual(lo, 2)
        self.assertIsNone(hi)

    def test_yoe_range_parses_min_and_max(self) -> None:
        text = "3-5 years of professional experience in cybersecurity is required."
        r = regex_rules.extract(text)
        self.assertEqual(r.years_experience_min, 3)
        self.assertEqual(r.years_experience_max, 5)

    def test_yoe_ignored_outside_experience_context(self) -> None:
        text = "Founded 25 years ago. Bachelor's degree required."
        r = regex_rules.extract(text)
        self.assertIsNone(r.years_experience_min)

    def test_degree_first_match_wins(self) -> None:
        self.assertEqual(regex_rules.extract("Bachelor's degree required").degree, "bachelor")
        self.assertEqual(regex_rules.extract("Associate's degree or equivalent experience").degree, "associate")
        self.assertEqual(regex_rules.extract("High school diploma required").degree, "high_school")
        self.assertEqual(regex_rules.extract("Equivalent work experience accepted").degree, "equivalent")

    def test_clearance_detection(self) -> None:
        self.assertEqual(regex_rules.extract("TS/SCI required.").clearance, "ts_sci")
        self.assertEqual(regex_rules.extract("Active Secret clearance required.").clearance, "secret")
        self.assertEqual(regex_rules.extract("Public Trust clearance preferred.").clearance, "public_trust")

    def test_salary_range_parsing(self) -> None:
        r = regex_rules.extract("Salary: $55,000 - $75,000 per year.")
        self.assertEqual(r.salary_min, 55000)
        self.assertEqual(r.salary_max, 75000)

        r2 = regex_rules.extract("Compensation $90K to $120K depending on experience.")
        self.assertEqual(r2.salary_min, 90000)
        self.assertEqual(r2.salary_max, 120000)

    def test_schedule_signals_captured(self) -> None:
        r = regex_rules.extract("Rotating shift, on-call coverage required, 24/7 operations.")
        self.assertIn("rotating", r.schedule_signals)
        self.assertIn("on_call", r.schedule_signals)
        self.assertIn("24x7", r.schedule_signals)

    def test_named_technical_skills(self) -> None:
        text = "Hands-on Splunk experience, comfortable with PowerShell and Microsoft Sentinel, Active Directory admin."
        r = regex_rules.extract(text)
        for skill in ["Splunk", "PowerShell", "Microsoft Sentinel", "Active Directory"]:
            self.assertIn(skill, r.technical_skills)


# ---------------------------------------------------------------------------
# LLM extractor (network-free)
# ---------------------------------------------------------------------------


class LlmExtractionTests(TestCase):
    def test_parse_json_tolerates_code_fences(self) -> None:
        text = '```json\n{"responsibilities": ["a"], "technical_skills": [], "seniority_signal": "entry", "remote_arrangement": "remote"}\n```'
        parsed = _parse_json(text)
        self.assertEqual(parsed["seniority_signal"], "entry")

    def test_cache_hit_skips_network(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            cache_dir = Path(tmpdir) / "extractions"
            cache_dir.mkdir(parents=True, exist_ok=True)
            listing = make_listing(
                title="Junior SOC Analyst",
                company="Acme",
                location="Remote",
                description="Triage alerts in Splunk.",
            )
            payload = {
                "responsibilities": ["Triage SIEM alerts"],
                "technical_skills": ["Splunk"],
                "seniority_signal": "entry",
                "remote_arrangement": "remote",
            }
            (cache_dir / f"{listing.listing_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            extractor = FakeClaudeExtractor(cache_dir=cache_dir)
            base = regex_rules.extract(listing.description)
            enriched, warnings = extractor.enrich(listing, base)

        self.assertEqual(extractor.calls, [])  # cache hit, no model call
        self.assertEqual(warnings, [])
        self.assertEqual(enriched.seniority_signal, "entry")
        self.assertEqual(enriched.remote_arrangement, "remote")
        self.assertIn("Splunk", enriched.technical_skills)

    def test_missing_api_key_falls_back_to_base(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            cache_dir = Path(tmpdir) / "extractions"
            extractor = ClaudeExtractor(api_key=None, cache_dir=cache_dir)
            listing = make_listing(
                title="Help Desk",
                company="Acme",
                location="Remote",
                description="Security+ required, 2+ years experience.",
            )
            base = regex_rules.extract(listing.description)
            enriched, warnings = extractor.enrich(listing, base)

        self.assertFalse(enriched.llm_used)
        self.assertEqual(enriched.certifications, base.certifications)
        self.assertTrue(any("ANTHROPIC_API_KEY" in w for w in warnings))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class ScoringTests(TestCase):
    def test_tabulate_groups_by_bucket_and_counts_certs(self) -> None:
        listings = [
            self._listing("SOC Analyst", description="Security+ required."),
            self._listing("Junior SOC Analyst", description="Security+ and Network+ required."),
            self._listing("Help Desk Technician", description="A+ required."),
        ]
        for listing in listings:
            listing.extracted = regex_rules.extract(listing.description)

        stats = tabulate(listings)
        self.assertEqual(stats["junior_soc"].sample_size, 2)
        self.assertEqual(stats["help_desk_it_admin"].sample_size, 1)
        soc_cert_dict = dict(stats["junior_soc"].certifications)
        self.assertEqual(soc_cert_dict.get("Security+"), 2)
        self.assertEqual(soc_cert_dict.get("Network+"), 1)

    @staticmethod
    def _listing(title: str, *, description: str) -> Listing:
        return make_listing(title=title, company="Acme", location="Remote", description=description)


# ---------------------------------------------------------------------------
# Reporting (markdown render)
# ---------------------------------------------------------------------------


class ReportingTests(TestCase):
    def test_markdown_includes_bucket_headers_and_certs(self) -> None:
        listings = [
            make_listing(
                title="Junior SOC Analyst", company="Acme", location="Remote", description="Security+ required."
            ),
        ]
        for listing in listings:
            listing.extracted = regex_rules.extract(listing.description)
        stats = tabulate(listings)

        md = render_markdown_report(
            generated_at="2026-05-24T00:00:00Z",
            tool_version="0.1.0",
            stats_by_bucket=stats,
            prior_stats_by_bucket=None,
            warnings=[],
        )

        self.assertIn("# Job Market Intel", md)
        self.assertIn("## Junior SOC Analyst", md)
        self.assertIn("Security+", md)


# ---------------------------------------------------------------------------
# Pipeline — end-to-end with fake collectors and fake LLM
# ---------------------------------------------------------------------------


class PipelineEndToEndTests(TestCase):
    def _fixture_listings(self) -> list[Listing]:
        return [
            make_listing(
                title="Junior SOC Analyst",
                company="Acme Security",
                location="Remote",
                description=(
                    "Triage SIEM alerts in Splunk. Security+ required, CySA+ preferred. "
                    "2+ years of professional experience in cybersecurity. "
                    "Bachelor's degree in IT or equivalent experience. Rotating shift."
                ),
                source="indeed",
                url="https://indeed.test/1",
            ),
            make_listing(
                title="SOC Analyst Tier 1",
                company="Beta Corp",
                location="Tampa, FL",
                description=(
                    "Monitor Microsoft Sentinel for security incidents. Network+ and Security+ required. "
                    "1 year of experience in IT or security. Associate's degree preferred. On-call rotation."
                ),
                source="linkedin",
                url="https://linkedin.test/1",
            ),
            # Duplicate of the first, different source — should collapse.
            make_listing(
                title="Junior SOC Analyst",
                company="acme security",
                location="remote",
                description="(shorter copy)",
                source="zip_recruiter",
                url="https://zip.test/1",
            ),
            make_listing(
                title="Help Desk Technician",
                company="Gamma Co",
                location="Dallas, TX",
                description=(
                    "Provide Tier 1 support via ServiceNow. A+ required, ITIL preferred. "
                    "Active Directory, Office 365. High school diploma required. Salary $45,000 - $55,000."
                ),
                source="indeed",
                url="https://indeed.test/2",
            ),
            make_listing(
                title="Desktop Support",
                company="Delta Inc",
                location="Remote",
                description=(
                    "Image laptops, manage Group Policy, troubleshoot Microsoft 365 issues. "
                    "Network+ preferred. 1-2 years of experience in technical support."
                ),
                source="glassdoor",
                url="https://glass.test/1",
            ),
        ]

    def test_pipeline_runs_with_fake_collectors_and_fake_llm(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            cache_dir = tmp / "cache"
            output_dir = tmp / "out"

            collector = FakeCollector(source_name="indeed", listings=self._fixture_listings())
            llm = FakeClaudeExtractor(cache_dir=cache_dir / "extractions")

            options = PipelineOptions(
                min_description_chars=0,
                location="United States",
                also_remote=False,
                results_per_source=10,
                role_buckets=["junior_soc", "help_desk_it_admin"],
                use_llm=True,
                cache_dir=cache_dir,
                output_dir=output_dir,
                dry_run=False,
                today="2026-05-24",
            )
            pipeline = Pipeline(collectors=[collector], llm_extractor=llm, options=options)
            snapshot = pipeline.run()

            # Files written
            self.assertTrue((output_dir / "snapshot-2026-05-24.json").exists())
            self.assertTrue((output_dir / "snapshot-2026-05-24.csv").exists())
            self.assertTrue((output_dir / "report-2026-05-24.md").exists())
            self.assertTrue((output_dir / "trend.csv").exists())

            self.assertEqual(snapshot["schema_version"], "1.0")
            # 5 raw → 4 after dedup (the duplicate Acme SOC Analyst collapses).
            self.assertEqual(snapshot["summary"]["total_listings_pre_dedup"], 5)
            self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 4)
            self.assertGreaterEqual(snapshot["summary"]["listings_with_llm_extraction"], 1)

            bucket_stats = snapshot["stats_by_bucket"]
            self.assertIn("junior_soc", bucket_stats)
            self.assertIn("help_desk_it_admin", bucket_stats)

            soc_certs = {entry[0]: entry[1] for entry in bucket_stats["junior_soc"]["certifications"]}
            self.assertIn("Security+", soc_certs)

    def test_dry_run_does_not_write_outputs(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            output_dir = tmp / "out"
            collector = FakeCollector(source_name="indeed", listings=self._fixture_listings())

            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=output_dir,
                    dry_run=True,
                    today="2026-05-24",
                ),
            )
            pipeline.run()
            self.assertFalse((output_dir / "snapshot-2026-05-24.json").exists())

    def test_cli_main_runs_with_only_greenhouse_and_no_llm(self) -> None:
        # Smoke test the CLI wiring without invoking the network; we patch GreenhouseCollector
        # to a fake that returns empty results.
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)

            class _EmptyGreenhouse:
                source_name = "greenhouse"

                def __init__(self, *, company_slugs, timeout: float = 30.0) -> None:
                    self.company_slugs = company_slugs

                def collect(self, *, queries, location, results_per_query, freshness_days=14):
                    return CollectorResult(source_name="greenhouse")

            with (
                patch("job_market_intel.cli.GreenhouseCollector", _EmptyGreenhouse),
                patch(
                    "job_market_intel.cli.load_credentials",
                    return_value=(
                        type("C", (), {"usajobs_email": None, "usajobs_api_key": None, "anthropic_api_key": None})(),
                        [],
                    ),
                ),
            ):
                from job_market_intel.cli import main

                exit_code = main(
                    [
                        "--sites",
                        "greenhouse",
                        "--no-llm",
                        "--no-remote-pass",
                        "--no-1password",
                        "--cache-dir",
                        str(tmp / "cache"),
                        "--output-dir",
                        str(tmp / "out"),
                        "--today",
                        "2026-05-24",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((tmp / "out" / "snapshot-2026-05-24.json").exists())


# ---------------------------------------------------------------------------
# Models — quick sanity that asdict round-trips
# ---------------------------------------------------------------------------


class ModelTests(TestCase):
    def test_listing_asdict_includes_extracted(self) -> None:
        listing = make_listing(title="Junior SOC Analyst", company="Acme", location="Remote", description="x")
        listing.extracted = ExtractedRequirements(certifications=["Security+"], llm_used=False)
        payload = asdict(listing)
        self.assertEqual(payload["extracted"]["certifications"], ["Security+"])


# ---------------------------------------------------------------------------
# Freshness filter (_is_fresh + pipeline integration)
# ---------------------------------------------------------------------------


class FreshnessFilterTests(TestCase):
    def test_is_fresh_keeps_recent_iso8601(self) -> None:
        from datetime import UTC, datetime, timedelta

        from job_market_intel.pipeline import _is_fresh

        recent = (datetime.now(UTC) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self.assertTrue(_is_fresh(recent, max_age_days=14))

    def test_is_fresh_drops_stale_iso8601(self) -> None:
        from datetime import UTC, datetime, timedelta

        from job_market_intel.pipeline import _is_fresh

        old = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        self.assertFalse(_is_fresh(old, max_age_days=14))

    def test_is_fresh_keeps_recent_date_only_greenhouse_style(self) -> None:
        from datetime import UTC, datetime, timedelta

        from job_market_intel.pipeline import _is_fresh

        recent = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()  # "2026-05-20"
        self.assertTrue(_is_fresh(recent, max_age_days=14))

    def test_is_fresh_drops_stale_date_only(self) -> None:
        self.assertFalse(__import__("job_market_intel.pipeline", fromlist=["_is_fresh"])._is_fresh("2024-01-01", 14))

    def test_is_fresh_keeps_listings_with_no_posted_at(self) -> None:
        from job_market_intel.pipeline import _is_fresh

        # Permissive: don't drop listings the source didn't date.
        self.assertTrue(_is_fresh(None, 14))
        self.assertTrue(_is_fresh("", 14))

    def test_is_fresh_keeps_unparseable_posted_at(self) -> None:
        from job_market_intel.pipeline import _is_fresh

        # If we can't parse the date, default to keep (don't drop on parser miss).
        self.assertTrue(_is_fresh("not-a-date", 14))
        self.assertTrue(_is_fresh("yesterday", 14))

    def test_pipeline_drops_stale_listings_after_dedup(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            fresh = make_listing(title="Junior SOC Analyst", company="Acme", location="Remote", description="x")
            fresh.posted_at = "2026-05-25"  # within 14 days of "today" in test
            stale = make_listing(title="Junior SOC Analyst", company="Beta", location="Remote", description="y")
            stale.posted_at = "2025-01-01"  # over a year old
            no_date = make_listing(title="Help Desk Technician", company="Gamma", location="Remote", description="z")
            no_date.posted_at = None  # missing — must be kept

            collector = FakeCollector(source_name="indeed", listings=[fresh, stale, no_date])

            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=tmp / "out",
                    dry_run=True,
                    today="2026-05-25",
                    freshness_days=14,
                ),
            )
            snapshot = pipeline.run()

        # 3 raw → after freshness filter, only fresh + no_date remain.
        self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 2)
        self.assertEqual(snapshot["summary"]["dropped_stale"], 1)


# ---------------------------------------------------------------------------
# USAJobs DatePosted param
# ---------------------------------------------------------------------------


class UsaJobsParamsTests(TestCase):
    def test_collector_passes_DatePosted_in_query(self) -> None:
        from unittest.mock import patch

        from job_market_intel.collectors.usajobs import USAJobsCollector

        seen_params = {}

        class _FakeResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"SearchResult": {"SearchResultItems": [], "SearchResultCountAll": 0}}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

            def get(self, _url, params=None):
                seen_params.update(params or {})
                return _FakeResp()

        with patch("job_market_intel.collectors.usajobs.httpx.Client", _FakeClient):
            collector = USAJobsCollector(email="x@example.com", api_key="k")
            collector.collect(
                queries=["soc analyst"],
                location="United States",
                results_per_query=0,
                freshness_days=14,
            )

        self.assertIn("DatePosted", seen_params)
        self.assertEqual(seen_params["DatePosted"], 14)

    def test_collector_clamps_freshness_to_usajobs_max_60(self) -> None:
        from unittest.mock import patch

        from job_market_intel.collectors.usajobs import USAJobsCollector

        seen = {}

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"SearchResult": {"SearchResultItems": [], "SearchResultCountAll": 0}}

        class _C:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

            def get(self, _u, params=None):
                seen.update(params or {})
                return _R()

        with patch("job_market_intel.collectors.usajobs.httpx.Client", _C):
            USAJobsCollector(email="x", api_key="k").collect(
                queries=["q"], location="United States", results_per_query=0, freshness_days=9999
            )

        self.assertEqual(seen.get("DatePosted"), 60)


# ---------------------------------------------------------------------------
# Seniority filter at pipeline level
# ---------------------------------------------------------------------------


class PipelineSeniorityFilterTests(TestCase):
    def _make(self, title: str, *, role: str = "junior_soc") -> Listing:
        return Listing(
            listing_id=f"id_{title.lower().replace(' ', '_')}",
            title=title,
            company="Acme",
            location="Remote",
            description="x",
            role_bucket=role,
            sources=["indeed"],
            source_urls=[],
            posted_at=None,
            fetched_at="2026-05-25",
        )

    def test_pipeline_keeps_only_entry_and_unclear_by_default(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            listings = [
                self._make("Junior SOC Analyst"),
                self._make("SOC Analyst"),  # unclear
                self._make("Senior SOC Analyst"),
                self._make("Director of Security Operations"),
                self._make("SOC Analyst II"),
            ]
            collector = FakeCollector(source_name="indeed", listings=listings)
            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=tmp / "out",
                    dry_run=True,
                    today="2026-05-25",
                ),
            )
            snapshot = pipeline.run()

        # Defaults: entry + unclear allowed → 2 kept (Junior + bare SOC Analyst).
        self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 2)
        self.assertEqual(snapshot["summary"]["dropped_seniority"], 3)

    def test_pipeline_widening_seniority_keeps_more(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            listings = [
                self._make("Junior SOC Analyst"),
                self._make("SOC Analyst"),
                self._make("Senior SOC Analyst"),
                self._make("Director of Security Operations"),
                self._make("SOC Analyst II"),
            ]
            collector = FakeCollector(source_name="indeed", listings=listings)
            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=tmp / "out",
                    dry_run=True,
                    today="2026-05-25",
                    allowed_seniority=["entry", "mid", "unclear"],
                ),
            )
            snapshot = pipeline.run()

        # Now entry + mid + unclear → 3 kept (Junior + SOC Analyst + SOC Analyst II).
        self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 3)

    def test_pipeline_drops_unclassified_by_default(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            on_topic = self._make("Junior SOC Analyst", role="junior_soc")
            off_topic = self._make("Junior Marketing Coordinator", role="unclassified")
            collector = FakeCollector(source_name="indeed", listings=[on_topic, off_topic])
            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=tmp / "out",
                    dry_run=True,
                    today="2026-05-25",
                ),
            )
            snapshot = pipeline.run()

        self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 1)
        self.assertEqual(snapshot["summary"]["dropped_off_topic"], 1)

    def test_pipeline_includes_unclassified_when_toggled(self) -> None:
        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            listings = [
                self._make("Junior SOC Analyst", role="junior_soc"),
                self._make("Software Engineer", role="unclassified"),
            ]
            collector = FakeCollector(source_name="indeed", listings=listings)
            pipeline = Pipeline(
                collectors=[collector],
                llm_extractor=None,
                options=PipelineOptions(
                    min_description_chars=0,
                    also_remote=False,
                    use_llm=False,
                    cache_dir=tmp / "cache",
                    output_dir=tmp / "out",
                    dry_run=True,
                    today="2026-05-25",
                    include_unclassified=True,
                ),
            )
            snapshot = pipeline.run()

        # Software Engineer title classifies as "unclear" seniority — kept.
        self.assertEqual(snapshot["summary"]["total_listings_post_dedup"], 2)


# ---------------------------------------------------------------------------
# Reclassify existing snapshot
# ---------------------------------------------------------------------------


class ClaudeCliExtractorTests(TestCase):
    """Test the subprocess-backed CLI LLM extractor without invoking real `claude`."""

    def _make_listing(self) -> Listing:
        return make_listing(
            title="Cyber Security Analyst",
            company="Acme",
            location="Remote",
            description="Monitor SIEM, triage alerts. 2+ years experience preferred.",
        )

    def test_parse_inner_json_strips_markdown_fences(self) -> None:
        from job_market_intel.extract.cli_llm import _parse_inner_json

        fenced = (
            "```json\n"
            '{"responsibilities":["x"],"technical_skills":[],'
            '"seniority_signal":"entry","remote_arrangement":"remote"}\n'
            "```\n\nSome rationale text"
        )
        result = _parse_inner_json(fenced)
        self.assertEqual(result["seniority_signal"], "entry")

    def test_parse_inner_json_handles_bare_json(self) -> None:
        from job_market_intel.extract.cli_llm import _parse_inner_json

        result = _parse_inner_json(
            '{"responsibilities":["x"],"technical_skills":[],'
            '"seniority_signal":"senior","remote_arrangement":"hybrid"}'
        )
        self.assertEqual(result["seniority_signal"], "senior")

    def test_parse_inner_json_raises_on_no_json(self) -> None:
        from job_market_intel.extract.cli_llm import _parse_inner_json

        with self.assertRaises(ValueError):
            _parse_inner_json("hello world no json here")

    def test_enrich_cache_hit_skips_subprocess(self) -> None:
        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            cache_dir = Path(tmpdir) / "extractions"
            cache_dir.mkdir(parents=True, exist_ok=True)
            listing = self._make_listing()
            cached_payload = {
                "responsibilities": ["Triage SIEM alerts"],
                "technical_skills": ["Splunk"],
                "seniority_signal": "entry",
                "remote_arrangement": "remote",
            }
            (cache_dir / f"{listing.listing_id}.json").write_text(json.dumps(cached_payload), encoding="utf-8")

            extractor = ClaudeCliExtractor(cache_dir=cache_dir)
            base = regex_rules.extract(listing.description)
            with patch("subprocess.run") as mock_run:
                enriched, warns = extractor.enrich(listing, base)
                self.assertFalse(mock_run.called)  # cache hit — no subprocess

        self.assertEqual(warns, [])
        self.assertEqual(enriched.seniority_signal, "entry")
        self.assertEqual(enriched.remote_arrangement, "remote")
        self.assertIn("Splunk", enriched.technical_skills)

    def test_enrich_invokes_subprocess_on_cache_miss(self) -> None:
        from unittest.mock import MagicMock

        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            extractor = ClaudeCliExtractor(cache_dir=Path(tmpdir) / "extractions")
            listing = self._make_listing()
            base = regex_rules.extract(listing.description)

            # The claude CLI emits an outer envelope with `result` being the model's text.
            fake_outer = {
                "is_error": False,
                "result": json.dumps(
                    {
                        "responsibilities": ["Triage"],
                        "technical_skills": ["Splunk"],
                        "seniority_signal": "entry",
                        "remote_arrangement": "remote",
                    }
                ),
            }
            fake_completed = MagicMock(returncode=0, stdout=json.dumps(fake_outer), stderr="")

            with patch("subprocess.run", return_value=fake_completed) as mock_run:
                enriched, warns = extractor.enrich(listing, base)

            self.assertTrue(mock_run.called)
            self.assertEqual(warns, [])
            self.assertEqual(enriched.seniority_signal, "entry")
            self.assertTrue(enriched.llm_used)

            # Cache should now have the payload — second call must NOT re-invoke.
            with patch("subprocess.run") as mock_run2:
                enriched2, _ = extractor.enrich(listing, base)
                self.assertFalse(mock_run2.called)
            self.assertEqual(enriched2.seniority_signal, "entry")

    def test_enrich_returns_base_on_subprocess_failure(self) -> None:
        from unittest.mock import MagicMock

        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            extractor = ClaudeCliExtractor(cache_dir=Path(tmpdir) / "extractions")
            listing = self._make_listing()
            base = regex_rules.extract(listing.description)

            failed = MagicMock(returncode=1, stdout="", stderr="claude crashed")
            with patch("subprocess.run", return_value=failed):
                enriched, warns = extractor.enrich(listing, base)

        # Returns base unchanged when subprocess fails.
        self.assertEqual(enriched.certifications, base.certifications)
        self.assertFalse(enriched.llm_used)
        self.assertTrue(any("cli_llm call failed" in w for w in warns))

    def test_enrich_returns_base_on_filenotfound(self) -> None:
        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            extractor = ClaudeCliExtractor(cache_dir=Path(tmpdir) / "extractions")
            listing = self._make_listing()
            base = regex_rules.extract(listing.description)

            with patch("subprocess.run", side_effect=FileNotFoundError("claude not found")):
                enriched, warns = extractor.enrich(listing, base)

        self.assertFalse(enriched.llm_used)
        self.assertTrue(any("claude CLI not found" in w or "cli_llm call failed" in w for w in warns))

    def test_enrich_many_parallel_returns_in_input_order(self) -> None:
        from unittest.mock import MagicMock

        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            extractor = ClaudeCliExtractor(cache_dir=Path(tmpdir) / "extractions", concurrency=3)
            listings = [
                make_listing(
                    title=f"Cyber Security Analyst {i}",
                    company=f"Co{i}",
                    location="Remote",
                    description=f"Desc {i}",
                )
                for i in range(5)
            ]
            bases = [regex_rules.extract(li.description) for li in listings]

            def _fake_run(cmd, **kwargs):
                payload = {
                    "is_error": False,
                    "result": json.dumps(
                        {
                            "responsibilities": ["x"],
                            "technical_skills": [],
                            "seniority_signal": "entry",
                            "remote_arrangement": "remote",
                        }
                    ),
                }
                return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

            with patch("subprocess.run", side_effect=_fake_run):
                enriched_list, warns = extractor.enrich_many(listings, bases)

        self.assertEqual(len(enriched_list), 5)
        self.assertEqual(warns, [])
        for e in enriched_list:
            self.assertEqual(e.seniority_signal, "entry")
            self.assertTrue(e.llm_used)


class ClaudeCliBatchAndPatternMiningTests(TestCase):
    """Tests for the batched LLM path + the pattern-mining helpers introduced
    in Round 7 (175-unclear cleanup + self-improving regex)."""

    def test_resolve_claude_invocation_uses_override(self) -> None:
        from job_market_intel.extract.cli_llm import _resolve_claude_invocation

        argv = _resolve_claude_invocation("/custom/path/to/claude")
        self.assertEqual(argv, ["/custom/path/to/claude"])

    def test_resolve_claude_invocation_falls_back_to_which(self) -> None:
        from job_market_intel.extract import cli_llm

        with (
            patch("job_market_intel.extract.cli_llm.Path.exists", return_value=False),
            patch("job_market_intel.extract.cli_llm.shutil.which", return_value=None),
        ):
            argv = cli_llm._resolve_claude_invocation()
        self.assertEqual(argv, ["claude"])

    def test_parse_inner_json_array_strips_fences_and_prose(self) -> None:
        from job_market_intel.extract.cli_llm import _parse_inner_json_array

        text = (
            "Sure, here is the result:\n"
            "```json\n"
            '[{"id":"a","seniority_signal":"entry","level_clues":["0-2 years"]},'
            '{"id":"b","seniority_signal":"senior","level_clues":["5+ years"]}]\n'
            "```\nThanks!"
        )
        parsed = _parse_inner_json_array(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "a")
        self.assertEqual(parsed[1]["seniority_signal"], "senior")

    def test_parse_inner_json_array_raises_on_no_array(self) -> None:
        from job_market_intel.extract.cli_llm import _parse_inner_json_array

        with self.assertRaises(ValueError):
            _parse_inner_json_array("no array in here")

    def test_enrich_unclear_batch_collects_clues_and_caches(self) -> None:
        from unittest.mock import MagicMock

        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            cache_dir = Path(tmpdir) / "extractions"
            extractor = ClaudeCliExtractor(cache_dir=cache_dir, concurrency=1)
            listings = [
                make_listing(
                    title="Systems Administrator",
                    company="Co1",
                    location="Remote",
                    description="Five years administering Windows servers.",
                ),
                make_listing(
                    title="IT Support Specialist",
                    company="Co2",
                    location="Remote",
                    description="Entry-level helpdesk role, 0-2 years experience.",
                ),
            ]
            bases = [ExtractedRequirements(), ExtractedRequirements()]

            id0, id1 = listings[0].listing_id, listings[1].listing_id
            fake_outer = {
                "is_error": False,
                "result": json.dumps(
                    [
                        {
                            "id": id0,
                            "seniority_signal": "senior",
                            "remote_arrangement": "remote",
                            "level_clues": ["five years administering"],
                            "technical_skills": ["Windows"],
                            "responsibilities": ["Admin"],
                        },
                        {
                            "id": id1,
                            "seniority_signal": "entry",
                            "remote_arrangement": "remote",
                            "level_clues": ["0-2 years experience", "Entry-level helpdesk"],
                            "technical_skills": [],
                            "responsibilities": ["Triage tickets"],
                        },
                    ]
                ),
            }
            fake_completed = MagicMock(returncode=0, stdout=json.dumps(fake_outer), stderr="")

            with patch("subprocess.run", return_value=fake_completed) as mock_run:
                enriched, clues, warns = extractor.enrich_unclear_batch(listings, bases, batch_size=10)

            self.assertTrue(mock_run.called)
            self.assertEqual(warns, [])
            self.assertEqual(len(enriched), 2)
            self.assertEqual(enriched[0].seniority_signal, "senior")
            self.assertEqual(enriched[1].seniority_signal, "entry")
            self.assertIn("five years administering", clues["senior"])
            self.assertIn("0-2 years experience", clues["entry"])
            # Per-listing cache was populated by the batched path.
            self.assertTrue((cache_dir / f"{id0}.json").exists())
            self.assertTrue((cache_dir / f"{id1}.json").exists())

    def test_merge_fills_yoe_degree_clearance_only_when_base_blank(self) -> None:
        """LLM-extracted scalars fill regex gaps but NEVER overwrite regex values."""
        from job_market_intel.extract.cli_llm import _merge

        # Case 1: base has all blanks → LLM fills everything.
        base = ExtractedRequirements()
        payload = {
            "seniority_signal": "entry",
            "remote_arrangement": "remote",
            "technical_skills": ["Splunk"],
            "responsibilities": ["Triage alerts"],
            "certifications": ["Security+", "CCNA"],
            "years_experience_min": 2,
            "years_experience_max": 4,
            "degree": "bachelor",
            "clearance": "secret",
        }
        out = _merge(base, payload)
        self.assertEqual(out.years_experience_min, 2)
        self.assertEqual(out.years_experience_max, 4)
        self.assertEqual(out.degree, "bachelor")
        self.assertEqual(out.clearance, "secret")
        self.assertEqual(set(out.certifications), {"Security+", "CCNA"})

        # Case 2: base has regex-extracted values → LLM does NOT overwrite scalars.
        base_with_data = ExtractedRequirements(
            years_experience_min=1,
            years_experience_max=3,
            degree="associate",
            clearance="public_trust",
            certifications=["A+"],
        )
        out2 = _merge(base_with_data, payload)
        # Scalars unchanged.
        self.assertEqual(out2.years_experience_min, 1)
        self.assertEqual(out2.years_experience_max, 3)
        self.assertEqual(out2.degree, "associate")
        self.assertEqual(out2.clearance, "public_trust")
        # Certifications are UNION (additive merge), not replace.
        self.assertEqual(set(out2.certifications), {"A+", "Security+", "CCNA"})

    def test_merge_skips_none_and_unspecified_sentinels(self) -> None:
        """LLM returning 'unspecified' degree or 'none' clearance must not fill in."""
        from job_market_intel.extract.cli_llm import _merge

        base = ExtractedRequirements()
        payload = {
            "seniority_signal": "entry",
            "remote_arrangement": "unspecified",
            "degree": "unspecified",
            "clearance": "none",
            "years_experience_min": None,
            "years_experience_max": None,
        }
        out = _merge(base, payload)
        self.assertIsNone(out.degree)
        self.assertIsNone(out.clearance)
        self.assertIsNone(out.years_experience_min)
        self.assertIsNone(out.years_experience_max)

    def test_enrich_full_batch_delegates_to_unclear_batch(self) -> None:
        """The full-pass wrapper should call the underlying batched method and
        return (enriched, warnings) — i.e. drop the clue dict the unclear path returns."""

        from job_market_intel.extract.cli_llm import ClaudeCliExtractor

        with WorkspaceTempDir() as tmpdir:
            extractor = ClaudeCliExtractor(cache_dir=Path(tmpdir) / "extractions")
            listings = [
                make_listing(title=f"Analyst {i}", company=f"Co{i}", location="Remote", description=f"Desc {i}")
                for i in range(3)
            ]
            bases = [ExtractedRequirements() for _ in listings]

            fake_enriched = [ExtractedRequirements(llm_used=True) for _ in listings]
            fake_clues = {"entry": [], "mid": [], "senior": [], "leadership": []}
            fake_warns = ["w1"]
            with patch.object(
                extractor,
                "enrich_unclear_batch",
                return_value=(fake_enriched, fake_clues, fake_warns),
            ) as mock_unclear:
                enriched, warns = extractor.enrich_full_batch(listings, bases)

            mock_unclear.assert_called_once()
            self.assertEqual(enriched, fake_enriched)
            self.assertEqual(warns, fake_warns)

    def test_pattern_miner_aggregates_and_filters_below_min_freq(self) -> None:
        from job_market_intel.pattern_mining import mine_patterns

        clues = {
            "entry": [
                "0-2 years experience",
                "0-2 years experience",
                "0-2 years experience",  # hits threshold (3x)
                "entry-level helpdesk role",
                "entry-level helpdesk role",  # only 2x — should be filtered
                "experience",  # stopword
            ],
            "senior": [
                "5+ years of experience",
                "5+ years of experience",
                "5+ years of experience",
                "lead the soc team",
            ],
            "mid": [],
            "leadership": [],
        }
        buckets = mine_patterns(clues, min_freq=3)
        # Entry: "0-2 years experience" should appear 3x; "entry-level helpdesk role" filtered (only 2x).
        entry_phrases = [p for p, _, _ in buckets["entry"]]
        self.assertIn("0-2 years experience", entry_phrases)
        self.assertNotIn("entry-level helpdesk role", entry_phrases)
        self.assertNotIn("experience", entry_phrases)
        # Senior: only the 3x phrase qualifies.
        senior_phrases = [p for p, _, _ in buckets["senior"]]
        self.assertEqual(senior_phrases, ["5+ years of experience"])
        # Empty buckets stay empty.
        self.assertEqual(buckets["mid"], [])

    def test_pattern_miner_markdown_renders_all_sections(self) -> None:
        from job_market_intel.pattern_mining import mine_patterns, render_markdown

        clues = {
            "entry": ["0-2 years experience"] * 3,
            "mid": [],
            "senior": ["5+ years of experience"] * 4,
            "leadership": ["set the strategy for the team"] * 3,
        }
        buckets = mine_patterns(clues, min_freq=3)
        md = render_markdown(buckets, generated_at="2026-05-26T00:00:00Z", source_count=10)
        self.assertIn("# Discovered seniority patterns", md)
        self.assertIn("## entry", md)
        self.assertIn("## mid", md)
        self.assertIn("## senior", md)
        self.assertIn("## leadership", md)
        self.assertIn("0-2 years experience", md)
        self.assertIn("5+ years of experience", md)
        # Empty section gets a fallback notice.
        self.assertIn("_No phrases met the frequency threshold._", md)


class ReclassifySnapshotTests(TestCase):
    def test_reclassify_filters_existing_snapshot(self) -> None:
        from job_market_intel.reclassify import reclassify_snapshot

        with WorkspaceTempDir() as tmpdir:
            tmp = Path(tmpdir)
            # Build a synthetic snapshot file with mixed seniority.
            snap = {
                "schema_version": "1.0",
                "generated_at": "2026-05-25T00:00:00Z",
                "tool_version": "0.1.0",
                "input": {},
                "summary": {"total_listings_pre_dedup": 5, "per_source_pre_dedup": {"indeed": 5}},
                "stats_by_bucket": {},
                "listings": [
                    {
                        "listing_id": f"id{i}",
                        "title": t,
                        "company": "Acme",
                        "location": "Remote",
                        "description": "",
                        "role_bucket": "junior_soc",
                        "sources": ["indeed"],
                        "source_urls": [],
                        "posted_at": None,
                        "fetched_at": "2026-05-25",
                        "extracted": None,
                    }
                    for i, t in enumerate(
                        [
                            "Junior SOC Analyst",
                            "SOC Analyst",
                            "Senior SOC Analyst",
                            "Director of Security Operations",
                            "Cybersecurity Analyst Intern",
                        ]
                    )
                ],
                "warnings": [],
            }
            input_path = tmp / "snapshot-2026-05-24.json"
            input_path.write_text(json.dumps(snap), encoding="utf-8")

            exit_code = reclassify_snapshot(
                input_path=input_path,
                output_dir=tmp / "out",
                freshness_days=60,
                allowed_seniority=["entry", "unclear"],
                include_unclassified=False,
                role_buckets=["junior_soc", "help_desk_it_admin"],
                today="2026-05-25",
                min_description_chars=0,
            )
            self.assertEqual(exit_code, 0)

            out_snap = json.loads((tmp / "out" / "snapshot-2026-05-25.json").read_text(encoding="utf-8"))

        # Junior + bare SOC Analyst + Intern = 3 kept. Senior dropped by seniority filter.
        # Director of Security Operations now goes to 'unclassified' via re-bucketing
        # (bare 'security operations' is no longer a SOC keyword), then off-topic dropped.
        self.assertEqual(out_snap["summary"]["total_listings_post_dedup"], 3)
        self.assertEqual(out_snap["summary"]["dropped_seniority"], 1)
        self.assertEqual(out_snap["summary"]["dropped_off_topic"], 1)  # Director re-bucketed → dropped
        titles_kept = {li["title"] for li in out_snap["listings"]}
        self.assertIn("Junior SOC Analyst", titles_kept)
        self.assertIn("Cybersecurity Analyst Intern", titles_kept)
        self.assertNotIn("Senior SOC Analyst", titles_kept)
        self.assertNotIn("Director of Security Operations", titles_kept)
