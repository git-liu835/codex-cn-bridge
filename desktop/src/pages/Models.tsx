import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';
import { useApp } from '../App';
import type { ModelConfig, TestResult } from '../types';

type ModelType = 'text' | 'vision' | 'image_gen' | 'video_gen';

const TYPE_LABELS: Record<ModelType, string[]> = {
  text:      ['文本', 'Text'],
  vision:    ['多模态(视觉)', 'Vision'],
  image_gen: ['图片生成', 'Image Gen'],
  video_gen: ['视频生成', 'Video Gen'],
};

function getModelType(m: ModelConfig): ModelType {
  if (m.is_image_gen) return 'image_gen';
  if (m.is_video_gen) return 'video_gen';
  if (m.is_multimodal) return 'vision';
  return 'text';
}

function typeToFlags(t: ModelType) {
  return {
    is_multimodal: t === 'vision',
    is_image_gen: t === 'image_gen',
    is_video_gen: t === 'video_gen',
  };
}

function groupByProvider(models: ModelConfig[]): Record<string, ModelConfig[]> {
  const groups: Record<string, ModelConfig[]> = {};
  models.forEach(m => {
    const key = m.provider || 'unknown';
    if (!groups[key]) groups[key] = [];
    groups[key].push(m);
  });
  return groups;
}

const EMPTY_FORM = {
  alias: '', target_model: '', provider: '', adapter: 'deepseek',
  base_url: '', api_key: '', api_key_env: '', enabled: true,
  modelType: 'text' as ModelType,
  vision_alias: '', image_gen_alias: '',
  advanced: { timeout: 120, max_retries: 0, tool_calls_enabled: true, extra_headers: {} as Record<string, string> },
};

const Models: React.FC = () => {
  const { tl, lang } = useApp();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [showModelForm, setShowModelForm] = useState(false);
  const [showCardForm, setShowCardForm] = useState(false);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [preSelProvider, setPreSelProvider] = useState('');
  const [modelType, setModelType] = useState<ModelType>('text');
  const [cardForm, setCardForm] = useState({
    provider: '', adapter: 'deepseek', base_url: '', api_key_env: '', api_key: '',
    alias: '', target: '', mtype: 'text' as ModelType,
  });
  const [modelForm, setModelForm] = useState({ ...EMPTY_FORM });

  const load = useCallback(async () => {
    try { const res = await api.getModels(); setModels(res.models || []); } catch { /* */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const allAliases = models.map(m => m.alias);
  const groups = groupByProvider(models);
  const providerNames = Object.keys(groups);
  const hasTextModel = models.some(m => getModelType(m) === 'text');

  // 多模型列表：找出有多个条目的 alias
  const aliasCounts: Record<string, number> = {};
  models.forEach(m => { aliasCounts[m.alias] = (aliasCounts[m.alias] || 0) + 1; });
  const multiModelAliases = new Set(Object.keys(aliasCounts).filter(a => aliasCounts[a] > 1));

  const resetModelForm = (provider: string) => {
    setModelForm({ ...EMPTY_FORM, provider, adapter: provider || 'deepseek' });
    setModelType('text');
    setTestResult(null);
  };

  // ═══ Model Form ═══════════════════════════════
  const openAddModel = (provider: string) => {
    setPreSelProvider(provider);
    setEditingAlias(null);
    resetModelForm(provider);
    setShowModelForm(true);
  };

  const openEditModel = (m: ModelConfig) => {
    setPreSelProvider('');
    setEditingAlias(m.alias);
    const mt = getModelType(m);
    setModelType(mt);
    setModelForm({
      alias: m.alias, target_model: m.target_model,
      provider: m.provider, adapter: m.adapter || m.provider,
      base_url: m.base_url, api_key: '', api_key_env: m.api_key_env,
      enabled: m.enabled, modelType: mt,
      vision_alias: m.vision_alias || '', image_gen_alias: m.image_gen_alias || '',
      advanced: { timeout: 120, max_retries: 0, tool_calls_enabled: true, extra_headers: {} },
      _index: m._index,
    } as any);
    setShowModelForm(true);
    setTestResult(null);
  };

  const handleSaveModel = async () => {
    if (!modelForm.alias || !modelForm.target_model) return;
    setLoading(true);
    try {
      const flags = typeToFlags(modelType);
      const data = {
        ...modelForm,
        ...flags,
        image_gen_alias: modelType === 'text' ? (modelForm.image_gen_alias || null) : null,
        vision_alias: (modelType === 'text') ? (modelForm.vision_alias || null) : null,
      };
      if (editingAlias) {
        const idx = (modelForm as any)._index;
        const qs = idx !== undefined ? `?_index=${idx}` : '';
        await api.updateModel(editingAlias, data, qs);
      } else {
        await api.addModel(data);
      }
      setShowModelForm(false);
      await load();
    } catch (err: any) {
      alert(tl('common.error') + ': ' + (err.message || err));
    } finally { setLoading(false); }
  };

  // ═══ Card Form (Add Provider) ═════════════════
  const openCardForm = () => {
    setCardForm({ provider: '', adapter: 'deepseek', base_url: '', api_key_env: '', api_key: '', alias: '', target: '', mtype: 'text' });
    setShowCardForm(true);
  };

  const handleAddCard = async () => {
    if (!cardForm.provider || !cardForm.alias || !cardForm.target) return;
    setLoading(true);
    try {
      const flags = typeToFlags(cardForm.mtype);
      await api.addModel({
        alias: cardForm.alias, target_model: cardForm.target,
        provider: cardForm.provider, adapter: cardForm.adapter,
        base_url: cardForm.base_url, api_key: cardForm.api_key,
        api_key_env: cardForm.api_key_env, enabled: true,
        ...flags,
      });
      setShowCardForm(false);
      await load();
    } catch (err: any) {
      alert(tl('common.error') + ': ' + (err.message || err));
    } finally { setLoading(false); }
  };

  // ═══ Inline Actions ═══════════════════════════
  const handleDelete = async (alias: string, idx?: number) => {
    if (!confirm(tl('models.confirmDelete'))) return;
    try {
      const qs = idx !== undefined ? `?_index=${idx}` : '';
      await api.deleteModel(alias, qs); await load();
    } catch (e: any) { alert(e.message); }
  };

  const handleTest = async (alias: string, idx?: number) => {
    setLoading(true);
    try {
      const data = idx !== undefined ? { _index: idx } : undefined;
      const r = await api.testConnection(alias, data);
      setTestResult(r);
    } catch (e: any) { setTestResult({ status: 'error', message: e.message }); }
    finally { setLoading(false); }
  };

  const handleQuickUpdate = async (alias: string, provider: string, fields: Record<string, any>, idx?: number) => {
    try {
      const qs = idx !== undefined ? `?_index=${idx}` : '';
      await api.updateModel(alias, { provider, ...fields }, qs); await load();
    } catch (e) { console.error(e); }
  };

  const handleActivate = async (alias: string, index: number) => {
    setLoading(true);
    try { await api.activateModel(alias, index); await load(); } catch (e: any) { alert(e.message); }
    finally { setLoading(false); }
  };

  const typeLabel = (t: ModelType) => lang === 'zh' ? TYPE_LABELS[t][0] : TYPE_LABELS[t][1];

  const adapterOptions = models[0]?.available_adapters?.length
    ? models[0].available_adapters
    : ['deepseek', 'qwen', 'kimi', 'doubao', 'zhipu'];

  const visionModels = models.filter(m => getModelType(m) === 'vision').map(m => m.alias);
  const imageGenModels = models.filter(m => getModelType(m) === 'image_gen').map(m => m.alias);

  // ═══ Render ════════════════════════════════════
  return (
    <div className="page">
      <div className="page-header">
        <h2>{tl('models.title')}</h2>
        <button className="btn btn-primary" onClick={openCardForm}>+ {tl('models.addCard')}</button>
      </div>

      {!hasTextModel && models.length > 0 && (
        <div className="warning-banner">
          {lang === 'zh' ? '请至少配置一个「文本」类型的模型，否则无法正常对话' : 'Please configure at least one "Text" type model for chat to work'}
        </div>
      )}

      {models.length === 0 && (
        <div className="card empty-card"><p className="muted">{tl('models.noModels')}</p></div>
      )}

      {/* ── Provider Cards ──────────────────────────── */}
      {providerNames.map(pName => {
        const pModels = groups[pName];
        const first = pModels[0];

        return (
          <div key={pName} className="provider-card">
            <div className="provider-card-header">
              <div className="provider-card-title">
                <span className="provider-avatar">{pName.charAt(0).toUpperCase()}</span>
                <span className="provider-name">{pName}</span>
                <span className="provider-adapter-tag">{first.adapter || pName}</span>
                {first.base_url && <span className="provider-url">{first.base_url}</span>}
              </div>
            </div>

            <div className="provider-card-meta">
              <span>API Key: {first.api_key_env || '—'}</span>
              <span className={`key-status ${first.api_key_set ? 'set' : ''}`}>
                {first.api_key_set ? '✓ Set' : '— Not set'}
              </span>
            </div>

            <div className="provider-models">
              <div className="models-header">
                {tl('models.modelsCount').replace('{n}', String(pModels.length))}
              </div>

              {pModels.map(m => {
                const mtype = getModelType(m);
                const isMulti = multiModelAliases.has(m.alias);
                const idx = m._index;
                return (
                  <div key={`${m.alias}-${idx ?? 0}-${m.provider}`} className={`model-row ${!m.enabled ? 'model-disabled' : ''}`}>
                    <label className="toggle-switch toggle-sm" title={m.enabled ? tl('common.enabled') : tl('common.disabled')}>
                      <input type="checkbox" checked={m.enabled}
                        onChange={e => handleQuickUpdate(m.alias, m.provider, { enabled: e.target.checked }, idx)} />
                      <span className="toggle-track"><span className="toggle-thumb" /></span>
                    </label>
                    <div className="model-main">
                      <span className="model-alias">{m.alias}</span>
                      {isMulti && m.enabled && (
                        <span className="model-active-badge" style={{ background: 'var(--green)', color: '#fff', fontSize: 10, padding: '1px 6px', borderRadius: 8, marginLeft: 4 }}>
                          {lang === 'zh' ? '当前' : 'Active'}
                        </span>
                      )}
                      <span className={`model-type-badge ${mtype.replace('_', '-')}`}>{typeLabel(mtype)}</span>
                      <span className="model-arrow">&rarr;</span>
                      <span className="model-target">{m.target_model}</span>
                    </div>

                    {/* Text model fallbacks */}
                    {mtype === 'text' && (
                      <div className="model-fallbacks">
                        {m.vision_alias && (
                          <span className="fallback-tag vision">
                            📷 {m.vision_alias}
                          </span>
                        )}
                        {m.image_gen_alias && (
                          <span className="fallback-tag img-gen">
                            🎨 {m.image_gen_alias}
                          </span>
                        )}
                      </div>
                    )}

                    <div className="model-actions">
                      {/* Switch button for inactive entries in multi-model aliases */}
                      {isMulti && !m.enabled && idx !== undefined && (
                        <button className="btn btn-sm btn-outline"
                          onClick={() => handleActivate(m.alias, idx)} disabled={loading}
                          style={{ color: 'var(--primary)', borderColor: 'var(--primary)' }}>
                          {lang === 'zh' ? '切换' : 'Switch'}
                        </button>
                      )}
                      <button className="btn btn-sm btn-outline"
                        onClick={() => handleTest(m.alias, idx)} disabled={loading}>
                        {tl('models.test')}
                      </button>
                      <button className="btn btn-sm" onClick={() => openEditModel(m)}>{tl('common.edit')}</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(m.alias, idx)}>{tl('common.delete')}</button>
                    </div>
                  </div>
                );
              })}
            </div>

            <button className="btn btn-sm btn-add-model" onClick={() => openAddModel(pName)}>
              + {tl('models.addModel')}
            </button>
          </div>
        );
      })}

      {testResult && (
        <div className={`test-result ${testResult.status}`} style={{ marginTop: 12 }}>
          {testResult.status === 'ok' ? '\u2713' : '\u2717'} {testResult.message}
          {testResult.elapsed_ms && ` (${testResult.elapsed_ms}ms)`}
        </div>
      )}

      {/* ═══ Model Form Modal ═══════════════════════ */}
      {showModelForm && (
        <div className="modal-overlay" onClick={() => setShowModelForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>{editingAlias ? tl('models.edit') : tl('models.addModel')}</h3>

            <div className="form-grid">
              <div className="form-group">
                <label>{tl('models.alias')}</label>
                <input value={modelForm.alias}
                  onChange={e => setModelForm({ ...modelForm, alias: e.target.value })}
                  placeholder="gpt-5-code" />
              </div>
              <div className="form-group">
                <label>{tl('models.target')}</label>
                <input value={modelForm.target_model}
                  onChange={e => setModelForm({ ...modelForm, target_model: e.target.value })}
                  placeholder="deepseek-v4-pro" />
              </div>

              <div className="form-group">
                <label>{tl('models.provider')}</label>
                <select value={modelForm.provider}
                  onChange={e => setModelForm({ ...modelForm, provider: e.target.value, adapter: e.target.value })}>
                  {providerNames.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label>{tl('models.adapter')}</label>
                <select value={modelForm.adapter}
                  onChange={e => setModelForm({ ...modelForm, adapter: e.target.value })}>
                  {adapterOptions.map((a: string) => <option key={a} value={a}>{a}</option>)}
                </select>
              </div>

              <div className="form-group full-width">
                <label>{tl('models.baseUrl')}</label>
                <input value={modelForm.base_url}
                  onChange={e => setModelForm({ ...modelForm, base_url: e.target.value })}
                  placeholder="https://api.deepseek.com/v1" />
              </div>

              <div className="form-group full-width">
                <label>Model Type</label>
                <select value={modelType} onChange={e => setModelType(e.target.value as ModelType)}>
                  {(Object.keys(TYPE_LABELS) as ModelType[]).map(t => (
                    <option key={t} value={t}>{typeLabel(t)}</option>
                  ))}
                </select>
              </div>

              {/* Text model fallback configs */}
              {modelType === 'text' && (
                <>
                  <div className="form-group">
                    <label>{tl('models.visionModel')}</label>
                    <select value={modelForm.vision_alias}
                      onChange={e => setModelForm({ ...modelForm, vision_alias: e.target.value })}>
                      <option value="">{tl('models.visionNone')}</option>
                      {visionModels.filter(a => a !== modelForm.alias).map(a => (
                        <option key={a} value={a}>{a}</option>
                      ))}
                    </select>
                  </div>
                  <div className="form-group">
                    <label>{tl('models.imageGenModel')}</label>
                    <select value={modelForm.image_gen_alias}
                      onChange={e => setModelForm({ ...modelForm, image_gen_alias: e.target.value })}>
                      <option value="">{tl('models.imageGenNone')}</option>
                      {imageGenModels.filter(a => a !== modelForm.alias).map(a => (
                        <option key={a} value={a}>{a}</option>
                      ))}
                    </select>
                  </div>
                </>
              )}

              <div className="form-group full-width">
                <label>{tl('models.apiKey')}</label>
                <div className="input-row">
                  <input type="password" value={modelForm.api_key}
                    onChange={e => setModelForm({ ...modelForm, api_key: e.target.value })}
                    placeholder={editingAlias ? '(不变则留空)' : '输入 API Key'} />
                </div>
              </div>
              <div className="form-group">
                <label>API Key Env</label>
                <input value={modelForm.api_key_env}
                  onChange={e => setModelForm({ ...modelForm, api_key_env: e.target.value })}
                  placeholder="DEEPSEEK_API_KEY" />
              </div>
              <div className="form-group">
                <label>{tl('common.enabled')}</label>
                <label className="checkbox-label">
                  <input type="checkbox" checked={modelForm.enabled}
                    onChange={e => setModelForm({ ...modelForm, enabled: e.target.checked })} />
                  {tl('common.enabled')}
                </label>
              </div>
            </div>

            <details className="advanced-section">
              <summary>{tl('models.advanced')}</summary>
              <div className="form-grid">
                <div className="form-group">
                  <label>{tl('models.timeout')}</label>
                  <input type="number" value={modelForm.advanced.timeout}
                    onChange={e => setModelForm({ ...modelForm, advanced: { ...modelForm.advanced, timeout: Number(e.target.value) } })} />
                </div>
                <div className="form-group">
                  <label>{tl('models.retries')}</label>
                  <input type="number" value={modelForm.advanced.max_retries}
                    onChange={e => setModelForm({ ...modelForm, advanced: { ...modelForm.advanced, max_retries: Number(e.target.value) } })} />
                </div>
                <div className="form-group">
                  <label>{tl('models.toolCalls')}</label>
                  <label className="checkbox-label">
                    <input type="checkbox" checked={modelForm.advanced.tool_calls_enabled}
                      onChange={e => setModelForm({ ...modelForm, advanced: { ...modelForm.advanced, tool_calls_enabled: e.target.checked } })} />
                    {tl('models.toolCalls')}
                  </label>
                </div>
              </div>
            </details>

            <div className="modal-actions">
              <button className="btn" onClick={() => setShowModelForm(false)}>{tl('common.cancel')}</button>
              <button className="btn btn-outline" onClick={() => handleTest(modelForm.alias)} disabled={loading}>
                {tl('models.test')}
              </button>
              <button className="btn btn-primary" onClick={handleSaveModel} disabled={loading}>
                {tl('common.save')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ═══ Card Form Modal (Add Provider) ════════ */}
      {showCardForm && (
        <div className="modal-overlay" onClick={() => setShowCardForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>{tl('models.addCard')}</h3>

            <div className="form-grid">
              <div className="form-group">
                <label>{tl('models.provider')}</label>
                <input value={cardForm.provider}
                  onChange={e => setCardForm({ ...cardForm, provider: e.target.value })}
                  placeholder="zhipu" />
              </div>
              <div className="form-group">
                <label>{tl('models.adapter')}</label>
                <select value={cardForm.adapter}
                  onChange={e => setCardForm({ ...cardForm, adapter: e.target.value })}>
                  {adapterOptions.map((a: string) => <option key={a} value={a}>{a}</option>)}
                </select>
              </div>
              <div className="form-group full-width">
                <label>{tl('models.baseUrl')}</label>
                <input value={cardForm.base_url}
                  onChange={e => setCardForm({ ...cardForm, base_url: e.target.value })}
                  placeholder="https://open.bigmodel.cn/api/paas/v4" />
              </div>
              <div className="form-group">
                <label>API Key Env</label>
                <input value={cardForm.api_key_env}
                  onChange={e => setCardForm({ ...cardForm, api_key_env: e.target.value })}
                  placeholder="ZHIPU_API_KEY" />
              </div>
              <div className="form-group">
                <label>{tl('models.apiKey')}</label>
                <input type="password" value={cardForm.api_key}
                  onChange={e => setCardForm({ ...cardForm, api_key: e.target.value })} />
              </div>

              <div className="form-group full-width" style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
                <label style={{ fontWeight: 600 }}>首个模型</label>
              </div>
              <div className="form-group">
                <label>{tl('models.alias')}</label>
                <input value={cardForm.alias}
                  onChange={e => setCardForm({ ...cardForm, alias: e.target.value })} />
              </div>
              <div className="form-group">
                <label>{tl('models.target')}</label>
                <input value={cardForm.target}
                  onChange={e => setCardForm({ ...cardForm, target: e.target.value })} />
              </div>
              <div className="form-group full-width">
                <label>Model Type</label>
                <select value={cardForm.mtype} onChange={e => setCardForm({ ...cardForm, mtype: e.target.value as ModelType })}>
                  {(Object.keys(TYPE_LABELS) as ModelType[]).map(t => (
                    <option key={t} value={t}>{typeLabel(t)}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="modal-actions">
              <button className="btn" onClick={() => setShowCardForm(false)}>{tl('common.cancel')}</button>
              <button className="btn btn-primary" onClick={handleAddCard} disabled={loading}>
                {tl('common.save')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Models;
