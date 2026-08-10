import { HostedWorkspaceJourney } from "@/components/onboarding/HostedWorkspaceJourney";

// Hosted customer journey (G18) — additive route. Renders the deterministic hosted-workspace journey.
// When the hosted feature is dark or the user isn't admitted, the backend journey endpoint 404s and the
// component fails closed to a neutral "not available yet" — no legacy account-creation path is offered here.
export default function HostedOnboardingPage() {
  return <HostedWorkspaceJourney />;
}
