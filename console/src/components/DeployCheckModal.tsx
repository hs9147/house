import { useEffect, useState } from 'react';
import Modal from './Modal';
import { api } from '../lib/api';
import type { BuildProfile, DeployCheckOut } from '../lib/types';

/**
 * 배포 전 점검 — 걸기 **전에** 알 수 있는 것을 먼저 보여 준다.
 *
 * 실패 진단은 실패한 뒤에 로그를 본다. 그런데 실패의 절반은 걸기 전에 이미 정해져 있었다:
 * 의존성 선언이 없거나(플랫폼이 설치할 근거가 없다), 소스가 하위 폴더인데 빌드 대상 폴더가
 * 비어 있거나, 저장한 기동 스크립트가 지금 규칙을 통과하지 못하거나. 그 셋이 실측에서
 * 배포를 죽였고, 화면에는 "서비스 시작 실패" 한 줄만 남았다.
 *
 * **배포를 막지는 않는다.** 감지가 못 맞히는 구성은 늘 있어서, 잠그면 "플랫폼이 틀렸는데
 * 배포도 못 하는" 상태가 된다. 점검은 말하고 배포는 사람이 결정한다.
 */
const BADGE: Record<string, { label: string; color: string }> = {
  ok: { label: '확인', color: '#7ee787' },
  warn: { label: '주의', color: '#e3b341' },
  fail: { label: '문제', color: '#f85149' },
};

export default function DeployCheckModal({
  projectId, profile, onClose,
}: {
  projectId: number;
  profile: BuildProfile;
  onClose: () => void;
}) {
  const [data, setData] = useState<DeployCheckOut | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    api.deployCheck(projectId, profile).then(
      (res) => { if (alive) setData(res); },
      (err: Error) => { if (alive) setError(err.message); },
    );
    // 팝업을 바로 닫으면 응답이 뒤늦게 와서 사라진 화면에 setState를 부른다.
    return () => { alive = false; };
  }, [projectId, profile]);

  return (
    <Modal title={`배포 전 점검 — ${profile}`} onClose={onClose}>
      {!data && !error && <p style={{ fontSize: 13 }}>리포와 설정을 보는 중…</p>}
      {error && <p className="error">{error}</p>}
      {data && (
        <>
          <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 8px' }}>
            실행 폴더: <span className="mono">{data.run_dir}</span>
            {data.detected && <> · 감지된 배포 단위: {data.detected}</>}
            {' · '}문제 {data.summary.fail} · 주의 {data.summary.warn} · 확인 {data.summary.ok}
          </p>
          <table>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.key}>
                  <td style={{ width: 52, verticalAlign: 'top' }}>
                    <span style={{ color: BADGE[item.status]?.color, fontSize: 12 }}>
                      {BADGE[item.status]?.label ?? item.status}
                    </span>
                  </td>
                  <td>
                    <div style={{ fontSize: 13 }}>{item.title}</div>
                    <div className="mutedtext" style={{ fontSize: 12 }}>{item.detail}</div>
                    {/* 고칠 것을 함께 적는다 — "문제"만 알려 주면 무엇을 해야 하는지 모른다. */}
                    {item.fix && item.status !== 'ok' && (
                      <div style={{ fontSize: 12, marginTop: 2 }}>→ {item.fix}</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* 규칙표는 아는 모양만 맞힌다 — 같은 사실을 기본 LLM에게 읽히고 "무엇이 더
              걸릴 것 같은가"를 받는다. 못 받으면 그 이유를 말한다(빈 칸으로 두면
              "문제 없음"으로 읽힌다). */}
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>
              LLM 판단{data.provider && ` — ${data.provider}`}
            </div>
            {data.advice ? (
              <pre
                className="mono"
                style={{
                  background: '#0d1117', padding: 10, borderRadius: 6, fontSize: 12,
                  maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0,
                }}
              >
                {data.advice}
              </pre>
            ) : (
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                {data.error || '판단을 받지 못했습니다.'}
              </p>
            )}
          </div>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 8 }}>
            점검은 아무것도 바꾸지 않고, 배포를 막지도 않습니다 — 감지가 못 맞히는 구성이
            있으므로 판단은 사람이 합니다.
          </p>
        </>
      )}
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
        <button className="secondary" onClick={onClose}>닫기</button>
      </div>
    </Modal>
  );
}
