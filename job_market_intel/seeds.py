"""Role search seeds, role-bucket classification, cert dictionary, company seed lists."""

from __future__ import annotations

import re
from typing import Literal

RoleBucket = Literal["junior_soc", "help_desk_it_admin", "unclassified"]

# Role search seeds, grouped by bucket. Each phrase becomes a search query
# against each enabled source.
ROLE_SEEDS: dict[RoleBucket, list[str]] = {
    "junior_soc": [
        "SOC analyst",
        "junior SOC analyst",
        "tier 1 SOC analyst",
        "security operations analyst",
        "cybersecurity analyst",
        "information security analyst",
    ],
    "help_desk_it_admin": [
        "help desk technician",
        "IT support specialist",
        "desktop support",
        "systems administrator",
        "IT administrator",
        "junior systems administrator",
    ],
}


def all_seed_phrases() -> list[str]:
    return [phrase for phrases in ROLE_SEEDS.values() for phrase in phrases]


# Title keyword rules for bucket classification. Order matters — first match wins.
# We classify on lowercased title.
_JUNIOR_SOC_KEYWORDS = (
    "soc analyst",
    "soc tier",
    "soc engineer",
    "tier 1 analyst",
    "tier i analyst",
    # 'security operations' alone is too broad — matches physical-security
    # technicians (state Capitol guards, building security, etc.). Require a
    # cyber-domain anchor: an explicit role suffix or the 'cyber'/'cybersecurity'
    # prefix.
    "security operations center",
    "security operations analyst",
    "security operations engineer",
    "security operations specialist",
    "cyber security operations",
    "cybersecurity operations",
    "cyber security analyst",
    "cybersecurity analyst",
    "information security analyst",
    "infosec analyst",
)

_HELP_DESK_KEYWORDS = (
    "help desk",
    "helpdesk",
    "service desk",
    "desktop support",
    "it support",
    "technical support",
    "systems admin",
    "system admin",
    "sysadmin",
    "it administrator",
    # Note: 'network administrator' deliberately excluded. Network admin is a
    # distinct mid-level IT role that requires separate tracking, not a help
    # desk / entry-tier role. Net-admin titles fall to 'unclassified' and are
    # filtered out by default (toggle with --include-unclassified).
)


# Non-IT departments that sometimes have their own "System Administrator" titles
# (Sales System Admin, Marketing System Admin, HR System Admin, etc.) — those are
# tooling/operations roles inside that department, NOT IT/cybersecurity roles.
# When one of these appears immediately before "system admin" we kick the listing
# out of the help_desk_it_admin bucket.
_NON_IT_DEPARTMENT_PREFIXES = (
    "sales",
    "marketing",
    "hr",
    "human resources",
    "finance",
    "accounting",
    "legal",
    "procurement",
    "operations",
    "customer success",
    "crm",
    "salesforce",
    "erp",
    "billing",
    "compensation",
    "benefits",
)


def _has_non_it_department_prefix(title_lower: str) -> bool:
    """True if title looks like '<non-IT dept> System Administrator' / 'Systems Admin'."""
    for dept in _NON_IT_DEPARTMENT_PREFIXES:
        # Match "Sales System Administrator" / "HR Systems Admin" / etc.
        if re.search(rf"\b{re.escape(dept)}\s+systems?\s+admin", title_lower):
            return True
    return False


def classify_role(title: str) -> RoleBucket:
    """Bucket a listing by its title. Falls back to 'unclassified'."""
    t = (title or "").lower()
    for kw in _JUNIOR_SOC_KEYWORDS:
        if kw in t:
            return "junior_soc"
    for kw in _HELP_DESK_KEYWORDS:
        if kw in t:
            # Reject non-IT department system-admin roles (Sales/Marketing/HR/etc.)
            if _has_non_it_department_prefix(t):
                return "unclassified"
            return "help_desk_it_admin"
    return "unclassified"


# ---------------------------------------------------------------------------
# Physical-security disambiguator
# ---------------------------------------------------------------------------
# Some listings titled "Security Operations Center Analyst" / "SOC Analyst"
# are actually about PHYSICAL building security (alarms, CCTV, guard duty,
# perimeter, patrol) — not cybersecurity. They share the "SOC" name but the
# domain is completely different. classify_role() can't tell from the title
# alone; we need the description body to disambiguate.

# Cyber-only terms — words that essentially never appear in a physical-
# security posting. Conservative on purpose: generic terms like "incident",
# "security event", "false positive", "tabletop exercise", "playbook" are
# excluded because they appear in physical security operations too.
# A listing that mentions ANY of these is treated as cyber and NOT relabeled.
_CYBER_DOMAIN_TERMS = (
    # SIEM / log platforms
    "siem",
    "splunk",
    "qradar",
    "sentinelone",
    "microsoft sentinel",
    "azure sentinel",
    "elastic stack",
    "elk stack",
    "sumo logic",
    "crowdstrike",
    # EDR / endpoint
    "edr",
    "mdr",
    "xdr",
    "endpoint detection",
    "endpoint security",
    # Threats / vulns
    "threat hunt",
    "threat intel",
    "threat actor",
    "malware",
    "ransomware",
    "phishing",
    "spear-phishing",
    "social engineering",
    "vulnerability scan",
    "vulnerability assessment",
    "cve-",
    "cvss",
    "ioc",
    "indicators of compromise",
    "yara",
    "sigma rule",
    "ttps",
    " apt ",  # spaces — APT is acronym, not standalone "apt" package
    "command and control",
    "exfiltration",
    "lateral movement",
    "privilege escalation",
    # Network security tech
    "ids",
    "ips",
    "firewall",
    "waf",
    "ddos",
    "intrusion detection system",
    "intrusion prevention",
    "soar",
    # Specific tooling
    "mitre",
    "att&ck",
    "attck",
    "kill chain",
    "packet capture",
    "pcap",
    "wireshark",
    "snort",
    "suricata",
    "zeek",
    # Identity / network
    "active directory",
    "azure ad",
    "entra id",
    "kerberos",
    "ldap",
    "powershell",
    # Practices
    "blue team",
    "red team",
    "purple team",
    "penetration test",
    "pen test",
    "forensics",
    "detection engineering",
    "detection rule",
    "data loss prevention",
    "dlp",
    # Generic-but-still-mostly-cyber
    "cybersecurity",
    "cyber security",
    "cyber threat",
    "cyber incident",
    "cyber defense",
    "infosec",
    "information security",
    "security operations center analyst",  # explicit cyber SOC titles
)

# Physical-security terms. Strong signal when no cyber terms present.
# (Most of these never appear in a cyber SOC posting.)
_PHYSICAL_SECURITY_TERMS = (
    "fixed security post",
    "security post",
    "guard duty",
    "security guard",
    "security officer",
    "patrol",
    "cctv",
    "closed circuit",
    "video surveillance",
    "video monitoring",
    "physical screening",
    "physical security",
    "perimeter security",
    "perimeter patrol",
    "alarm monitoring",
    "alarm system",
    "visitor management",
    "badging",
    "badge access",
    "site security",
    "building security",
    "campus security",
    "facility security",
    "physical access control",
    "armed",
    "unarmed",
    "metal detector",
    "x-ray screening",
    "loss prevention",
    "trespass",
    "dispatcher",
    "dispatching",
    "fixed post",
    "roving patrol",
    "duress",
    "panic button",
)


# ---------------------------------------------------------------------------
# Bureaucratic-empty filter — civil-service / school-district listings whose
# entire "description" is metadata fields (JobID, FLSA, Pay Grade, etc.) and
# zero job-content prose. Example: Guilford County Schools "SYSTEMS
# ADMINISTRATOR" — 478 chars of metadata, zero duties/requirements.
# ---------------------------------------------------------------------------

_METADATA_FIELD_TERMS = (
    "flsa",
    "pay grade",
    "salary schedule",
    "position type",
    "job id",
    "jobid",
    "bargaining unit",
    "civil service",
    "position term",
    "time basis",
    "date posted",
    "date available",
    "competitive appointment",
    "salary placement",
)

_TASK_LANGUAGE_TERMS = (
    "responsible for",
    "duties",
    "you will",
    "requirements:",
    "qualifications:",
    "essential functions",
    "we are seeking",
    "we're seeking",
    "must have",
    "your role",
    "key responsibilities",
    "what you'll do",
    "what you will do",
    "the ideal candidate",
    "in this role",
)


def is_bureaucratic_metadata_only(description: str | None, min_chars: int = 600) -> bool:
    """Return True if the description is mostly metadata fields with no job-content prose.

    Heuristic targets civil-service / school-district / similar listings that
    publish only the job classification metadata (JobID, FLSA, Pay Grade,
    Position Type, etc.) without an actual job description. Conservative —
    requires ALL of: short body + multiple metadata fields + zero task language.
    """
    text = (description or "").lower()
    if not text or len(text) >= min_chars:
        return False
    metadata_hits = sum(1 for term in _METADATA_FIELD_TERMS if term in text)
    task_hits = sum(1 for term in _TASK_LANGUAGE_TERMS if term in text)
    return metadata_hits >= 3 and task_hits == 0


_DECISIVE_PHYSICAL_TERMS = (
    # Surveillance hardware — never appears in a cyber-SOC posting.
    "cctv",
    "closed circuit",
    "video surveillance",
    "video monitoring",
    # Human-guard language — physical security only.
    "guard duty",
    "security guard",
    "security officer",
    "armed officer",
    "unarmed officer",
    "armed guard",
    "unarmed guard",
    # Screening hardware.
    "metal detector",
    "x-ray screening",
    "physical screening",
    # Patrol / post / building access — distinctively physical.
    "roving patrol",
    "perimeter patrol",
    "fixed security post",
    "fixed post",
    # Lifesafety hardware specific to physical security.
    "duress alarm",
    "panic button",
    # Retail / facility-specific.
    "loss prevention",
    "trespass",
)


def is_physical_security_role(description: str | None) -> bool:
    """Return True if a description appears to describe physical (not cyber) security.

    Rules, in order:

      1. **Decisive override**: if ANY decisive physical term appears (CCTV,
         "security guard", "metal detector", "roving patrol", etc.) the
         listing is physical-security, full stop. These terms literally
         never appear in a cyber-SOC posting; their presence is more
         informative than any number of cyber-flavored words elsewhere
         (e.g. "incident response" can mean either domain, but "CCTV"
         cannot).

      2. **Strict-majority fallback**: for listings without a decisive
         term, classify as physical when there are at least 2 physical
         terms AND the physical-term count strictly exceeds the cyber-term
         count. Catches descriptions that ring physical via aggregate
         signal (alarm + access control + intrusion detection) without
         using any of the decisive vocabulary.
    """
    text = (description or "").lower()
    if not text:
        return False
    # Tier 1: decisive override.
    if any(term in text for term in _DECISIVE_PHYSICAL_TERMS):
        return True
    # Tier 2: strict-majority fallback.
    physical_count = sum(1 for term in _PHYSICAL_SECURITY_TERMS if term in text)
    if physical_count < 2:
        return False
    cyber_count = sum(1 for term in _CYBER_DOMAIN_TERMS if term in text)
    return physical_count > cyber_count


# Compliance / GRC roles often get the "Information Security Analyst" title
# (which our SOC keyword list matches), but the actual work is policy / audit /
# framework administration — not SIEM monitoring or incident response. These
# postings shouldn't land in the junior_soc bucket.

# Terms that are HEAVILY GRC/compliance-flavored. None of these would appear
# in a normal SOC-monitoring posting except in passing.
_COMPLIANCE_EXCLUSIVE_TERMS = (
    "governance, risk, and compliance",
    "governance risk and compliance",
    " grc ",
    "(grc)",
    "compliance program",
    "compliance officer",
    "compliance framework",
    "compliance audit",
    "audit program",
    "internal audit",
    "internal control",
    "internal controls",
    "control testing",
    "control assessment",
    "regulatory compliance",
    "regulatory examination",
    "regulatory reporting",
    "sox compliance",
    "sox controls",
    "soc 2 audit",
    "soc 2 type ii",
    "pci-dss compliance",
    "pci dss compliance",
    "hipaa compliance",
    "hitrust",
    "iso 27001",
    "iso 27002",
    "iso/iec 27001",
    "nist 800-53",
    "nist csf",
    "nist cybersecurity framework",
    "policy development",
    "policy administration",
    "policy lifecycle",
    "risk assessment framework",
    "risk register",
    "third-party risk",
    "third party risk",
    "vendor risk",
    "vendor risk management",
    "trm",
    "gap analysis",
    "audit findings",
    "audit remediation",
    # DoD / federal RMF + STIG + assessment work — distinct job family from
    # SOC monitoring. These roles do system accreditation, control
    # implementation, and vulnerability/patch management — not alert triage.
    "risk management framework",
    "dod rmf",
    "disa stig",
    "stigs",
    "stig compliance",
    "stig hardening",
    "fedramp",
    "jsig",
    "joint sap implementation guide",
    "security impact analysis",
    "security impact assessment",
    "security control assessment",
    "security control implementation",
    "system security plan",
    "authority to operate",
    "authorization to operate",
    "continuous monitoring program",
    "vulnerability management program",
    "patch management program",
    "configuration validation",
    "devsecops",
)

# Operational-SOC terms that mean "this is real incident-response / monitoring
# work" even if compliance also gets mentioned. ANY of these tilts the call
# back to SOC.
_OPERATIONAL_SOC_TERMS = (
    "siem",
    "soar",
    "edr",
    "xdr",
    "splunk",
    "qradar",
    "sentinel",
    "crowdstrike",
    "alert triage",
    "alert tuning",
    "threat hunting",
    "incident response",
    "incident handling",
    "malware analysis",
    "log analysis",
    "log review",
    "detection engineering",
    "tier 1 analyst",
    "tier 2 analyst",
    "tier 3 analyst",
    "security operations center",
    "24x7 soc",
    "24/7 soc",
    "blue team",
)


def is_compliance_role(description: str | None) -> bool:
    """Return True if a 'security analyst'-titled posting is actually GRC/compliance work.

    Heuristic: 3+ compliance/GRC-exclusive terms appear in the description AND
    no operational-SOC term appears. The 3-hit threshold prevents single-mention
    false positives (a real SOC role might say "we audit our processes" in
    passing); requiring SIEM/EDR/IR/etc. to be absent prevents demoting real
    SOC roles that also happen to do compliance reporting.
    """
    text = (description or "").lower()
    if not text or len(text) < 200:
        return False
    if any(term in text for term in _OPERATIONAL_SOC_TERMS):
        return False
    compliance_hits = sum(1 for term in _COMPLIANCE_EXCLUSIVE_TERMS if term in text)
    return compliance_hits >= 3


# ---------------------------------------------------------------------------
# Seniority classification — applied after role bucketing to filter the
# "Senior SOC Analyst", "Director of Security Operations", etc. noise out
# of the entry-level dataset.
# ---------------------------------------------------------------------------

SeniorityBucket = Literal["entry", "mid", "senior", "leadership", "unclear"]

# Order: leadership > senior > mid > entry > unclear.
# Patterns are matched in this priority order — first match wins.
_LEADERSHIP_PATTERNS = (
    r"\bmanager\b",
    r"\bdirector\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bhead\s+of\b",
    r"\bchief\b",
    r"\barchitect\b",
    r"\bleadership\b",
    r"\bcio\b",
    r"\bciso\b",
)
_SENIOR_PATTERNS = (
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bexpert\b",
    r"\b(?:iii|iv|v)\b",
    r"\btier\s*(?:3|4|iii|iv)\b",
    r"\blevel\s*(?:3|4|iii|iv)\b",
    # Role-suffix level numbers: "System Administrator 3", "Engineer 4", etc.
    # Common in government / defense titles. Arabic numerals only — Roman is
    # covered by the \b(?:iii|iv|v)\b pattern above.
    r"\b(?:administrator|analyst|engineer|specialist|technician|architect|consultant|developer)\s+(?:3|4|5)\b",
    r"\s(?:3|4)\s*$",  # title ending in " 3" or " 4"
    # L3/L4/T3/T4 tier shorthand (followed by role-type word, end-of-title, or
    # punctuation — narrow to avoid matching "L3 cache" / random initials).
    r"\b[LT][-\s]?(?:3|4|5)\s+(?:soc|support|analyst|engineer|technician|specialist|administrator)\b",
    r",\s*[LT][-\s]?(?:3|4|5)\b",  # "Technical Support Agent, L3"
    # L3/L4 at end-of-title or before punctuation, preceded by a role word.
    r"\b(?:soc|support|analyst|engineer|technician|specialist|administrator)\s+[LT][-\s]?(?:3|4|5)\b(?=[\s,\-–(]|$)",  # noqa: RUF001
)
_MID_PATTERNS = (
    r"\bii\b",
    r"\btier\s*(?:2|ii)\b",
    r"\blevel\s*(?:2|ii)\b",
    r"\bintermediate\b",
    r"\bmid[-\s]?level\b",
    # "Administrator 2", "Engineer 2", etc.
    r"\b(?:administrator|analyst|engineer|specialist|technician|architect|consultant|developer)\s+2\b",
    r"\s2\s*$",  # title ending in " 2"
    # L2/T2 shorthand
    r"\b[LT][-\s]?2\s+(?:soc|support|analyst|engineer|technician|specialist|administrator)\b",
    r",\s*[LT][-\s]?2\b",
    # L2 at end-of-title or before punctuation, preceded by a role word.
    r"\b(?:soc|support|analyst|engineer|technician|specialist|administrator)\s+[LT][-\s]?2\b(?=[\s,\-–(]|$)",  # noqa: RUF001
    # Lowercase 'll' is a common typo/font-variant of 'II' — same shape, same
    # meaning. Catches "Service Desk Specialist ll" (Salt Lake County style).
    # Note: \bii\b is already case-insensitive but ii != ll; needs its own pattern.
    r"\b(?:administrator|analyst|engineer|specialist|technician|architect|consultant|developer|support)\s+ll\b(?=[\s,\-–(]|$)",  # noqa: RUF001
)
_ENTRY_PATTERNS = (
    r"\bjunior\b",
    r"\bjr\.?\b",
    r"\bentry[-\s]?level\b",
    r"\bentry\b",
    r"\bassociate\b",
    r"\bassoc\.?\b",
    r"\bintern\b",
    r"\btrainee\b",
    r"\bapprentice\b",
    r"\bnew\s+grad(?:uate)?\b",
    r"\brecent\s+grad(?:uate)?\b",
    r"\btier\s*(?:1|i)\b",
    r"\blevel\s*(?:1|i)\b",
    r"\bi\b\s*$",  # title ending in " I"
    r"\s1\s*$",  # title ending in " 1"
    # Mid-title roman / numeric level: "Help Desk Technician I - Rochester, NY"
    # The level token is followed by punctuation (dash/comma/parenthesis) and
    # is preceded by a role-type word so it doesn't false-match generic "I".
    r"\b(?:administrator|analyst|engineer|specialist|technician|architect|consultant|developer|support)\s+(?:i|1)\b(?=[\s,\-–(]|$)",  # noqa: RUF001
    # L1/T1 shorthand
    r"\b[LT][-\s]?1\s+(?:soc|support|analyst|engineer|technician|specialist|administrator)\b",
    r",\s*[LT][-\s]?1\b",  # "Technical Support Agent, L1"
    # L1 at end-of-title or before punctuation, preceded by a role word.
    r"\b(?:soc|support|analyst|engineer|technician|specialist|administrator)\s+[LT][-\s]?1\b(?=[\s,\-–(]|$)",  # noqa: RUF001
)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def classify_seniority(title: str) -> SeniorityBucket:
    """Bucket a title by seniority.

    Strict priority order: leadership > senior > mid > entry > unclear.
    A "Senior IT Manager" classifies as leadership (manager wins over senior).
    A title with no seniority modifier (bare "SOC Analyst") returns 'unclear'.
    """
    t = (title or "").strip()
    if not t:
        return "unclear"
    if _matches_any(t, _LEADERSHIP_PATTERNS):
        return "leadership"
    if _matches_any(t, _SENIOR_PATTERNS):
        return "senior"
    if _matches_any(t, _MID_PATTERNS):
        return "mid"
    if _matches_any(t, _ENTRY_PATTERNS):
        return "entry"
    return "unclear"


# ---------------------------------------------------------------------------
# Description-based seniority fallback — only applied when the title is
# 'unclear' (bare "SOC Analyst", "Cybersecurity Analyst", etc.).
# ---------------------------------------------------------------------------

# Strong senior signals in the description body. Conservative — false-positives
# here cost us a senior posting being filtered out, which is acceptable.
_DESC_SENIOR_PATTERNS = (
    # YoE phrasings — numeric
    r"\b(?:5|6|7|8|9|10|11|12|13|14|15|20)\s*\+\s*years?\b",  # "5+ years"
    r"\bminimum\s+of\s+(?:5|6|7|8|9|10|11|12|13|14|15|20)\s*years?\b",
    r"\bat\s+least\s+(?:5|6|7|8|9|10|11|12|13|14|15|20)\s*years?\b",
    r"\b(?:5|6|7|8|9|10|11|12|13|14|15|20)\s+or\s+more\s+years?\b",
    r"\b(?:5|6|7|8|9|10)\s*-\s*(?:7|8|9|10|12|15)\s*years?\b",  # "5-10 years"
    # YoE phrasings — words (for body sentences that don't use a literal digit "+")
    r"\b(?:5|6|7|8|9|10|11|12|13|14|15|20)\s+years?\s+of\s+(?:relevant|professional|industry|prior|hands[-\s]on|progressive|technical)?\s*(?:experience|background|work)\b",
    # Tier / level signals in body
    r"\btier\s*(?:3|iii|4|iv|5|v)\b",
    r"\blevel\s*(?:3|iii|4|iv|5|v)\b",
    # Leadership / scope language
    r"\blead(?:s|ing)?\s+(?:a|the|our|technical|junior)?\s*(?:team|engineers?|analysts?|staff)\b",
    r"\bmentor(?:s|ing|ship)?\s+(?:junior|other|team|new|engineers?|analysts?|staff)\b",
    r"\bcoach(?:es|ing)?\s+(?:junior|other|team|new)\b",
    r"\bsupervis(?:e|es|ing|ory)\b",
    r"\bsenior[-\s]level\b",
    r"\bprincipal\s+(?:engineer|analyst|architect|consultant)\b",
    r"\bstaff\s+(?:engineer|analyst|architect)\b",
    r"\bsubject[-\s]matter\s+expert\b",
    r"\bdeep\s+expertise\b",
    r"\bextensive\s+experience\b",
    r"\bsme\b",
    # Decision-making / strategy language
    r"\bset(?:ting|s)?\s+(?:the\s+|our\s+|a\s+)?strategy\b",
    r"\bdrive\s+(?:strategic|architectural)\b",
    # Round 7: "strategic direction" — owning/driving/setting the strategic
    # direction of anything is by definition not an entry-level responsibility.
    r"\bstrategic\s+direction\b",
    # Round 7: "sole contributor" — a solo senior IC who owns a domain end-to-end.
    # In job descriptions this anchors to senior+ scope (no team to lean on,
    # so the listing requires someone who can self-direct). Entry-level roles
    # are NOT described as sole-contributor positions.
    r"\bsole[-\s]contributor\b",
    # Round 6: explicit tier-3/L3 in description body
    r"\b(?:tier\s*3|level\s*3|l3)\s+(?:soc|support|analyst|engineer|technician|specialist|help\s+desk)\b",
    r"\b(?:soc|support|help\s+desk|service\s+desk)\s+(?:agent|specialist|analyst|technician)\s*,?\s*(?:tier\s*3|level\s*3|l3)\b",
    # Round 5: experience-level adjectives (require "of" to avoid generic
    # phrases like "demonstrated leadership" referring to the company).
    r"\bproven\s+(?:experience|expertise|track\s+record|history)\b",
    r"\bdemonstrated\s+(?:experience|expertise|leadership|ability\s+to\s+lead)\b",
    # "over N years" requires an experience/role context to filter out company
    # history phrases like "For over 20 years, we've set the standard".
    r"\bover\s+(?:5|6|7|8|9|10|11|12|15|20)\s+years?\s+(?:of\s+(?:experience|industry|professional|cybersecurity|security|it)|in\s+(?:cybersecurity|security|it|the\s+field))\b",
    # "deep/advanced knowledge of <technical domain>" — domain noun required to
    # filter out marketing copy like "deep knowledge of our customers' needs".
    r"\bdeep\s+(?:knowledge|understanding|expertise)\s+of\s+(?:networking|security|cybersecurity|systems?|tcp|protocols?|incident|threat|detection|siem|forensics|cryptography|malware|active\s+directory|cloud|aws|azure|gcp|firewalls?|intrusion|vulnerability|encryption|authentication|identity|kerberos|tls|ssl|powershell|python|bash|linux|windows|unix)\b",
    r"\badvanced\s+(?:knowledge|understanding)\s+of\s+(?:networking|security|cybersecurity|systems?|tcp|protocols?|incident|threat|detection|siem|forensics|cryptography|malware|active\s+directory|cloud|aws|azure|gcp|firewalls?|intrusion|vulnerability|encryption|authentication|identity|kerberos|tls|ssl|powershell|python|bash|linux|windows|unix)\b",
)

# Strong entry signals. More conservative since false-entry-positives directly
# mislead learners.
_DESC_ENTRY_PATTERNS = (
    # Explicit level language
    r"\bentry[-\s]level\b",
    r"\bjunior[-\s]level\b",
    r"\bjunior\s+(?:soc|security|cybersecurity|it|systems?|network|help[-\s]desk)\b",
    r"\bjunior\s+(?:analyst|engineer|administrator|specialist|technician)\b",
    r"\bassociate[-\s]level\b",
    r"\bstarter\s+(?:role|position)\b",
    # No-experience phrasings
    r"\bno\s+prior\s+experience\b",
    r"\bno\s+(?:previous\s+)?(?:security|cybersecurity|it|professional|work|industry)?\s*experience\s+(?:required|necessary)\b",
    r"\bexperience\s+(?:is\s+)?not\s+required\b",
    # Graduate / new-career phrasings
    r"\brecent\s+graduate\b",
    r"\bnew\s+grad(?:uate)?\b",
    r"\bnewly\s+graduated\b",
    r"\bfresh\s+grad(?:uate)?s?\b",
    r"\bcollege\s+grad(?:uate)?s?\b",
    r"\bgraduate\s+program\b",
    r"\bearly[-\s]career\b",
    r"\bfirst\s+(?:cybersecurity|security|it|soc|help\s+desk|professional)\s+(?:role|job|position)\b",
    # Tier / level signals in body
    r"\btier\s*(?:1|i)\b",
    r"\blevel\s*(?:1|i)\b",
    # Intern / apprentice / trainee in body
    r"\binternship\s+(?:program|opportunity)\b",
    r"\bapprentice(?:ship)?\b",
    r"\btrainee\s+(?:position|program|role)\b",
    # Tight YoE ranges that indicate entry
    r"\b0\s*-\s*2\s+years?\b",
    r"\b0\s+to\s+2\s+years?\b",
    r"\b1\s*-\s*2\s+years?\b",
    r"\b0\s*\+\s*years?\b",
    # Training language
    r"\btraining\s+will\s+be\s+provided\b",
    r"\bwe\s+will\s+train\b",
    r"\bon[-\s]the[-\s]job\s+training\b",
    r"\bno\s+(?:degree|certification)\s+required\b",
    # Round 6: explicit tier-1/L1 in description body
    r"\b(?:tier\s*1|level\s*1|l1)\s+(?:soc|support|analyst|engineer|technician|specialist|help\s+desk)\b",
    r"\b(?:soc|support|help\s+desk|service\s+desk)\s+(?:agent|specialist|analyst|technician)\s*,?\s*(?:tier\s*1|level\s*1|l1)\b",
    # Round 5: experience-level adjectives indicating basics (require "of" /
    # "with" / "in" to filter out generic "working knowledge" of business etc.).
    r"\bworking\s+knowledge\s+of\b",
    r"\bbasic\s+(?:knowledge|understanding|familiarity)\s+(?:of|with|in)\b",
    # Months instead of years
    r"\b(?:6|8|9|12|18|24)\s*\+?\s*months\s+of\s+(?:experience|work)\b",
    # Sub-year YoE phrasings
    r"\bup\s+to\s+(?:1|2|one|two)\s+years?\b",
    r"\bone\s+to\s+two\s+years?\b",
    # Round 6: "preferred but not required" and friends
    r"\b(?:experience|degree|certification)\s+(?:is\s+)?preferred\s+but\s+not\s+required\b",
    r"\bexperience\s+(?:is\s+)?not\s+required\b",
    r"\bexperience\s+is\s+(?:a\s+)?plus\b",
    # Round 6: front-line / first point of contact (role-word anchored to avoid
    # senior-management hits like 'serves as the first point of contact for executives')
    r"\bfront[-\s]?line\s+(?:technical\s+)?support\b",
    r"\b(?:agent|specialist|analyst|technician|representative)\s+(?:is\s+|serves\s+as\s+)?(?:the\s+)?first\s+point\s+of\s+contact\b",
    r"\bserves\s+as\s+(?:the\s+)?first\s+point\s+of\s+contact\s+for\s+(?:customers|users|end[-\s]users|clients)\b",
)


# Generic YoE extraction from description — used by classify_seniority_from_description
# when no explicit yoe_min is supplied. Looks for `N years` anywhere in the body
# but requires a context cue word within 60 characters to filter out unrelated
# matches like "founded 30 years ago".
_GENERIC_YOE_RE = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*years?",
    re.IGNORECASE,
)
# Stricter cue list for the BODY-SCAN fallback. Only phrases that directly tie
# a number to "years of experience" — not generic posting language like
# "required" / "preferred" which trigger on degree/cert requirements too and
# produce false positives ("Bachelor's degree required" + "25 years ago" elsewhere).
_YOE_CONTEXT_CUES = (
    "experience",
    "background",
    "exp.",
    "minimum",
    "at least",
    "or more",
    "of relevant",
    "of professional",
    "of prior",
    "of hands-on",
    "of progressive",
    "of cybersecurity",
    "of security",
    "of it",
    "looking for someone with",
    "ideal candidate has",
    "candidates with",
)


def _extract_yoe_from_description(text: str | None) -> int | None:
    """Find the smallest plausible 'N years' value in the description body.

    Scans every occurrence of `\\d+ years?` and keeps a match only if a cue word
    appears within 60 chars on either side ('experience', 'background', 'minimum',
    'looking for', etc.). Returns the smallest valid N in [1, 30] — typically the
    listing's stated minimum. Returns None if nothing plausible found.
    """
    if not text:
        return None
    body = text.lower()
    candidates: list[int] = []
    for match in _GENERIC_YOE_RE.finditer(body):
        try:
            n = int(match.group("n"))
        except (TypeError, ValueError):
            continue
        if n < 1 or n > 30:
            # Filter "0 years ago" type matches and "100 years of history".
            # Note: "0 years" itself is captured by the explicit entry pattern.
            continue
        start = max(0, match.start() - 60)
        end = min(len(body), match.end() + 60)
        window = body[start:end]
        if any(cue in window for cue in _YOE_CONTEXT_CUES):
            candidates.append(n)
    if not candidates:
        return None
    return min(candidates)


def _yoe_to_bucket(yoe_min: int | None) -> SeniorityBucket:
    """Bucket a numeric YoE_min into a seniority level.

    0-2 → entry (most SOC/IT 'Tier 1' postings)
    3-5 → mid
    6+  → senior
    None / negative → unclear
    """
    if yoe_min is None or yoe_min < 0:
        return "unclear"
    if yoe_min <= 2:
        return "entry"
    if yoe_min <= 5:
        return "mid"
    return "senior"


def classify_seniority_from_description(description: str | None, yoe_min: int | None = None) -> SeniorityBucket:
    """Best-effort seniority from a description body.

    Order of precedence:
      1. Strong senior keyword in body → senior (conservative, prevents mislabeling
         experienced roles as entry).
      2. Strong entry keyword in body → entry.
      3. Externally supplied YoE_min bucket (from regex_rules' strict extractor).
      4. Generic YoE extraction directly from the body — catches "5 years of
         relevant experience" type phrasings the strict extractor misses.
      5. Fall back to unclear.
    """
    text = description or ""
    if text and _matches_any(text, _DESC_SENIOR_PATTERNS):
        return "senior"
    if text and _matches_any(text, _DESC_ENTRY_PATTERNS):
        return "entry"
    if yoe_min is not None:
        return _yoe_to_bucket(yoe_min)
    # Fallback: try to extract YoE from the body ourselves.
    body_yoe = _extract_yoe_from_description(text)
    return _yoe_to_bucket(body_yoe)


def classify_seniority_combined(
    title: str,
    description: str | None = None,
    yoe_min: int | None = None,
) -> SeniorityBucket:
    """Title classifier first; description-aware fallback only when title is unclear.

    Title wins when it has an explicit signal (Senior/Junior/Director/etc).
    Bare titles like "SOC Analyst" route through the description classifier so
    we don't lose listings to the 'unclear' bucket when descriptions clearly
    indicate seniority.

    YoE veto: a listing asking for 3+ years of experience is never entry-level,
    no matter what the title says. Titles like "Specialist", "Analyst", or
    even "Junior X" paired with a 3+ year experience bar are de-facto mid+
    roles — the experience floor reflects the actual seniority. The veto
    only applies when the title would otherwise be entry/mid/unclear;
    explicit senior/leadership titles still win (someone called "Senior
    Engineer" with only 3 years of YoE listed is still a senior posting).
    """
    title_level = classify_seniority(title)

    # Senior / leadership titles are explicit and always authoritative.
    if title_level in ("senior", "leadership"):
        return title_level

    # YoE veto: 3+ years experience required → cannot be entry. Maps to
    # mid (3-5 yrs) or senior (6+ yrs) via _yoe_to_bucket.
    if yoe_min is not None and yoe_min >= 3:
        return _yoe_to_bucket(yoe_min)

    if title_level != "unclear":
        return title_level
    return classify_seniority_from_description(description, yoe_min)


# Certification dictionary. Each entry: canonical name + list of regex variants.
# Variants are matched case-insensitively as whole words/phrases.
CERTIFICATIONS: dict[str, list[str]] = {
    "Security+": [r"security\s*\+", r"sec\s*\+", r"comptia\s+security\s*\+?", r"comptia\s+security\s+plus"],
    "Network+": [r"network\s*\+", r"net\s*\+", r"comptia\s+network\s*\+?", r"comptia\s+network\s+plus"],
    "A+": [r"(?<![\w+])a\s*\+(?![\w+])", r"comptia\s+a\s*\+?", r"comptia\s+a\s+plus"],
    "CySA+": [r"cysa\s*\+", r"cybersecurity\s+analyst\s*\+?"],
    "PenTest+": [r"pentest\s*\+", r"penetration\s+tester\s*\+?"],
    "CASP+": [r"casp\s*\+", r"comptia\s+advanced\s+security\s+practitioner"],
    "CCNA": [r"\bccna\b"],
    "CCNP": [r"\bccnp\b"],
    "CCIE": [r"\bccie\b"],
    "CISSP": [r"\bcissp\b"],
    "CISA": [r"\bcisa\b"],
    "CISM": [r"\bcism\b"],
    "OSCP": [r"\boscp\b"],
    "OSEP": [r"\bosep\b"],
    "GSEC": [r"\bgsec\b"],
    "GCIH": [r"\bgcih\b"],
    "GCIA": [r"\bgcia\b"],
    "GCFA": [r"\bgcfa\b"],
    "GREM": [r"\bgrem\b"],
    "GPEN": [r"\bgpen\b"],
    "GMON": [r"\bgmon\b"],
    "ITIL": [r"\bitil\b"],
    "MS-900": [r"\bms[-\s]?900\b"],
    "AZ-104": [r"\baz[-\s]?104\b"],
    "AZ-500": [r"\baz[-\s]?500\b"],
    "AZ-900": [r"\baz[-\s]?900\b"],
    "SC-200": [r"\bsc[-\s]?200\b"],
    "SC-900": [r"\bsc[-\s]?900\b"],
    "AWS Certified Cloud Practitioner": [r"aws\s+certified\s+cloud\s+practitioner", r"\bccp\b"],
    "AWS Certified Security": [r"aws\s+certified\s+security"],
    "AWS Certified Solutions Architect": [r"aws\s+certified\s+solutions\s+architect"],
    "Splunk Core Certified User": [r"splunk\s+core\s+certified\s+user"],
    "Splunk Certified Power User": [r"splunk\s+(?:core\s+)?certified\s+power\s+user"],
    "Splunk Certified Admin": [r"splunk\s+certified\s+admin(?:istrator)?"],
    "CompTIA Linux+": [r"comptia\s+linux\s*\+?", r"linux\s*\+"],
    "MCSA": [r"\bmcsa\b"],
    "MCSE": [r"\bmcse\b"],
    "RHCSA": [r"\brhcsa\b"],
    "RHCE": [r"\brhce\b"],
    "Google Cybersecurity Certificate": [r"google\s+cybersecurity\s+(?:professional\s+)?certificate"],
    "HDI Desktop Support Technician": [r"hdi\s+desktop\s+support\s+technician", r"\bhdi-dst\b"],
}


# Technical-skill keywords (the dictionary half; LLM catches long tail).
TECH_SKILLS: dict[str, list[str]] = {
    "Splunk": [r"\bsplunk\b"],
    "Microsoft Sentinel": [r"microsoft\s+sentinel", r"azure\s+sentinel"],
    "QRadar": [r"\bqradar\b"],
    "Elastic / ELK": [r"\belastic\s+stack\b", r"\belk\s+stack\b", r"\bkibana\b", r"\blogstash\b"],
    "CrowdStrike": [r"crowdstrike", r"\bfalcon\b"],
    "SentinelOne": [r"sentinelone", r"sentinel\s*one"],
    "Microsoft Defender": [r"microsoft\s+defender", r"defender\s+for\s+endpoint"],
    "Wireshark": [r"\bwireshark\b"],
    "PowerShell": [r"\bpowershell\b"],
    "Python": [r"\bpython\b"],
    "Bash": [r"\bbash\b"],
    "Active Directory": [r"active\s+directory", r"\bAD\s+DS\b"],
    "Linux": [r"\blinux\b", r"\bubuntu\b", r"\brhel\b", r"\bcentos\b"],
    "TCP/IP": [r"\btcp/ip\b", r"\btcp\s*/\s*ip\b"],
    "MITRE ATT&CK": [r"mitre\s+att&?ck", r"\bATT&?CK\b"],
    "Ticketing (ServiceNow/Jira)": [r"servicenow", r"\bjira\b", r"\bremedy\b", r"\bzendesk\b"],
    "Office 365 / M365": [r"\bo365\b", r"office\s+365", r"\bm365\b", r"microsoft\s+365"],
    "VMware": [r"\bvmware\b", r"\bvsphere\b", r"\besxi\b"],
    "Group Policy": [r"group\s+policy", r"\bGPO\b"],
}


# Greenhouse company slugs — all verified to return HTTP 200 from
# https://boards-api.greenhouse.io/v1/boards/{slug}/jobs. Each company hires
# SOC analysts, IT support, or technical support roles at least occasionally.
# Find more public boards at https://boards.greenhouse.io/{slug} (visit in
# browser to verify before adding here).
GREENHOUSE_COMPANIES: list[str] = [
    # Cybersecurity vendors (smaller hiring volume but high topic relevance)
    "huntress",
    "expel",
    "cybereason",
    "knowbe4",
    "tanium",
    "censys",
    "recordedfuture",
    "tines",
    "bishopfox",
    "synack",
    "bugcrowd",
    "dragos",
    # Larger tech / observability companies with sizable IT/support teams
    "cloudflare",
    "okta",
    "datadog",
    "elastic",
    "sumologic",
    "newrelic",
    "gitlab",
    "databricks",
    "jamf",
]

# Lever company slugs — currently empty. Most cybersecurity employers we
# checked either don't use Lever publicly or have migrated to other ATSes
# (Workday, Greenhouse, Ashby). Add slugs here as you find them by visiting
# https://jobs.lever.co/{slug} in a browser. Format: one slug per line.
LEVER_COMPANIES: list[str] = []


def normalize_phrase(phrase: str) -> str:
    """Loose normalization for dedup & comparison (lowercase, collapse whitespace, strip punctuation)."""
    cleaned = re.sub(r"[^\w\s]", " ", (phrase or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


# ---------------------------------------------------------------------------
# US-location classification — used by Greenhouse/Lever collectors to filter
# out clearly-non-US listings while keeping ambiguous "Remote" postings.
# ---------------------------------------------------------------------------

# Two-letter US state codes + DC + common territories. Matched against word
# boundaries so "CA" matches "Austin, CA" but not "OCAML".
_US_STATE_CODES = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "PR",
        "GU",
        "VI",
    }
)

# Substrings that, if present, clearly indicate a non-US location.
# Lowercase. Order matters only for performance.
_NON_US_MARKERS = (
    # Regional shorthand
    "emea",
    "apac",
    "anz",
    "latam",
    "mena",
    "united kingdom",
    " uk)",
    " uk,",
    ", uk",
    "london",
    "edinburgh",
    "manchester",
    "ireland",
    "dublin",
    "belfast",
    "germany",
    "berlin",
    "munich",
    "frankfurt",
    "hamburg",
    "france",
    "paris",
    "lyon",
    "spain",
    "madrid",
    "barcelona",
    "portugal",
    "lisbon",
    "porto",
    "italy",
    "milan",
    "rome",
    "netherlands",
    "amsterdam",
    "rotterdam",
    "the hague",
    "belgium",
    "brussels",
    "switzerland",
    "zurich",
    "geneva",
    "austria",
    "vienna",
    "sweden",
    "stockholm",
    "gothenburg",
    "norway",
    "oslo",
    "denmark",
    "copenhagen",
    "finland",
    "helsinki",
    "poland",
    "warsaw",
    "krakow",
    "wroclaw",
    "czech",
    "prague",
    "hungary",
    "budapest",
    "romania",
    "bucharest",
    "ukraine",
    "kyiv",
    "estonia",
    "latvia",
    "lithuania",
    "russia",
    "moscow",
    "st. petersburg",
    "greece",
    "athens",
    "turkey",
    "istanbul",
    "israel",
    "tel aviv",
    "uae",
    "dubai",
    "abu dhabi",
    "saudi arabia",
    "riyadh",
    "india",
    "bangalore",
    "bengaluru",
    "mumbai",
    "delhi",
    "hyderabad",
    "pune",
    "chennai",
    "gurgaon",
    "noida",
    "pakistan",
    "karachi",
    "lahore",
    "islamabad",
    "singapore",
    "philippines",
    "manila",
    "cebu",
    "vietnam",
    "hanoi",
    "ho chi minh",
    "thailand",
    "bangkok",
    "indonesia",
    "jakarta",
    "malaysia",
    "kuala lumpur",
    "japan",
    "tokyo",
    "osaka",
    "korea",
    "seoul",
    "china",
    "shanghai",
    "beijing",
    "shenzhen",
    "guangzhou",
    "hong kong",
    "taiwan",
    "taipei",
    "australia",
    "sydney",
    "melbourne",
    "brisbane",
    "perth",
    "new zealand",
    "auckland",
    "wellington",
    "south africa",
    "johannesburg",
    "cape town",
    "nigeria",
    "lagos",
    "kenya",
    "nairobi",
    "egypt",
    "cairo",
    "brazil",
    "são paulo",
    "sao paulo",
    "rio de janeiro",
    "argentina",
    "buenos aires",
    "chile",
    "santiago",
    "colombia",
    "bogota",
    "mexico",
    "mexico city",
    "canada",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "calgary",
    "edmonton",
)

# Substrings that, if present, are strong positive signals of US-only.
_US_POSITIVE_MARKERS = (
    "united states",
    "usa",
    "u.s.",
    " u.s ",
    "(us)",
    "remote - us",
    "remote, us",
    "remote (us",
    "us-remote",
    "us remote",
    "americas",
    "north america",
)

_STATE_TOKEN_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")


def is_us_location(location: str | None) -> bool:
    """Classify a free-text location string as US-friendly (True) or not.

    Returns True for:
      - Anything containing a US state code (Austin, TX) or US positive marker
      - Bare "Remote" / "" / None — ambiguous, default to include
      - Multi-location strings where at least one segment is US

    Returns False for:
      - Any non-US country / city marker present
      - With a caveat: a US state code anywhere outranks a non-US marker for
        multi-location strings ("Remote (US, UK, Germany)" → True).

    Tuned to be inclusive on ambiguous cases — we'd rather show a London role
    that slipped through than drop a "Remote - Americas" role.
    """
    if not location:
        return True
    s = location.lower()

    # Strong US positive — always keep.
    if any(marker in s for marker in _US_POSITIVE_MARKERS):
        return True
    # US state code present — keep (covers "Austin, TX", "Remote - CA", etc.)
    if _STATE_TOKEN_RE.search(location) and any(
        token in _US_STATE_CODES for token in _STATE_TOKEN_RE.findall(location)
    ):
        return True
    # Otherwise: clearly non-US markers disqualify; ambiguous defaults to include.
    return not any(marker in s for marker in _NON_US_MARKERS)
