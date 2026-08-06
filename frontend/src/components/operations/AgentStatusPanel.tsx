"use client";

import React from "react";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/broker/StatusBadge";
import {
  operatorAgentView,
  operatorStateLabel,
  alertSeverityView,
  type OperatorAgentStatus,
} from "@/lib/agent-status";

/** Minimum-hardening WS-G — read-only OPERATOR panel for validation-agent status. Staff-only surface
 * (mount behind the existing operator gate). Renders mapped StatusViews, never a raw backend enum, and
 * shows ONLY the sanitised operator fields the backend presenter emits — there is no key/token/env/
 * credential/stack-trace in the DTO, so none can be rendered. The CUSTOMER equivalent lives elsewhere and
 * shows availability only. */
export const AgentStatusPanel: React.FC<{ status: OperatorAgentStatus | null }> = ({ status }) => {
  if (!status) {
    return (
      <Card>
        <div className="text-sm text-gray-500">Validation agent status unavailable.</div>
      </Card>
    );
  }
  const supervisedLabel =
    status.supervised === true ? "supervised" : status.supervised === false ? "UNSUPERVISED" : "unknown";
  return (
    <Card>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold">Validation agent</h3>
        <StatusBadge view={operatorAgentView(status.band)} title={operatorStateLabel(status.state)} />
      </div>
      <dl className="text-xs grid grid-cols-2 gap-1">
        <dt className="text-gray-500">State</dt>
        <dd>{operatorStateLabel(status.state)}</dd>
        <dt className="text-gray-500">Supervised</dt>
        <dd className={status.supervised === false ? "text-red-600 font-semibold" : ""}>{supervisedLabel}</dd>
        {status.reason ? (
          <>
            <dt className="text-gray-500">Reason</dt>
            <dd>{status.reason}</dd>
          </>
        ) : null}
      </dl>
      {status.alerts?.length ? (
        <div className="mt-3">
          <div className="text-xs text-gray-500 mb-1">Active alerts</div>
          <ul className="space-y-1">
            {status.alerts.map((a, i) => (
              <li key={`${a.name}-${i}`} className="flex items-center gap-2 text-xs">
                <StatusBadge view={alertSeverityView(a.severity)} />
                <span className="font-mono">{a.name}</span>
                <span className="text-gray-500">→ {a.runbook}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="mt-3 text-xs text-green-600">No active alerts.</div>
      )}
    </Card>
  );
};

export default AgentStatusPanel;
