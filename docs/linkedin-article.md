A referral is easy to send and easy to lose. Between "please see cardiology" and a booked visit sit an insurance check, a calendar search and a reminder, and each hand-off is a queue.

The problem: access teams spend their day on the routine path, so the hard cases (a rejected member ID, a patient free only on Saturdays, an urgent referral with no slot in time) wait.

Why it matters: a referral that never becomes a visit is delayed care, and nobody is accountable for it.

We built agents for the routine path and kept people on the exceptions:
FHIR ServiceRequest → X12 270/271 eligibility → slot search and constraint solver → reminders → human queues → audit and metrics

How it works:
1. Intake routes referral codes to a bookable service and sets an SLA by priority.
2. Eligibility sends a real 270 through a clearinghouse and reads the 271. Rejections and prior-auth flags become work items; nothing is auto-denied.
3. A deterministic solver ranks FHIR slots against provider, location, visit type, the patient's hours in their time zone, lead time and deadline, and says which constraint blocked it.
4. Agents call it over MCP or A2A. An agent's booking waits for a person, and nobody approves their own request.
5. Reminders respect quiet hours, back off on failure and escalate when exhausted. Gemini drafts the wording from facts with no patient identifiers.

Practices: a signed audit chain before any state change; patients see only their own records; If-Match on slots so two bookers cannot win; fail closed when identity or payers are down.

Tradeoffs: approvals cost staff time; strict 271 reading sends more cases to people; X12 is the plumbing every payer integration still needs.

Value: staff time moves to judgement work, and every stalled referral has an owner and a reason.

As agents spread across health systems, the rule is not "how much can it do alone" but "who answers for this decision".

Where would you let an agent book without a human: never, or only for routine follow-ups?

github.com/kogunlowo123/patient-access-agents

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #HealthIT #FHIR #PatientAccess
