import os
import sys
import glob
import pandas as pd
import numpy as np
import scipy.stats as stats
import statsmodels.api as sm
import statsmodels.formula.api as smf
from statsmodels.stats.multicomp import pairwise_tukeyhsd
import matplotlib.pyplot as plt
import seaborn as sns
import json

# Setup matplotlib style
plt.style.use('seaborn-v0_8-whitegrid')
plt.rcParams['font.sans-serif'] = 'Helvetica'
plt.rcParams['axes.edgecolor'] = '#cccccc'
plt.rcParams['axes.linewidth'] = 0.8

print("=== INICIANDO PROCESSAMENTO DE DADOS DATASUS 2024 ===")

# 1. Mapeamento de UFs para Macrorregiões
uf_to_region = {
    11: 'Norte', 12: 'Norte', 13: 'Norte', 14: 'Norte', 15: 'Norte', 16: 'Norte', 17: 'Norte',
    21: 'Nordeste', 22: 'Nordeste', 23: 'Nordeste', 24: 'Nordeste', 25: 'Nordeste', 26: 'Nordeste', 27: 'Nordeste', 28: 'Nordeste', 29: 'Nordeste',
    31: 'Sudeste', 32: 'Sudeste', 33: 'Sudeste', 35: 'Sudeste',
    41: 'Sul', 42: 'Sul', 43: 'Sul',
    50: 'Centro-Oeste', 51: 'Centro-Oeste', 52: 'Centro-Oeste', 53: 'Centro-Oeste'
}

uf_to_name = {
    11: 'RO', 12: 'AC', 13: 'AM', 14: 'RR', 15: 'PA', 16: 'AP', 17: 'TO',
    21: 'MA', 22: 'PI', 23: 'CE', 24: 'RN', 25: 'PB', 26: 'PE', 27: 'AL', 28: 'SE', 29: 'BA',
    31: 'MG', 32: 'ES', 33: 'RJ', 35: 'SP',
    41: 'PR', 42: 'SC', 43: 'RS',
    50: 'MS', 51: 'MT', 52: 'GO', 53: 'DF'
}

# 2. Carregamento e amostragem estratificada dos 12 meses de 2024
files = sorted(glob.glob('data/RD2024*.csv'))
print(f"Encontrados {len(files)} arquivos CSV mensais.")

sampled_chunks = []
# Coletar amostragem sistemática de cada arquivo para representatividade perfeita
for f in files:
    # Vamos ler em blocos e filtrar
    chunk_list = []
    for chunk in pd.read_csv(f, sep=';', chunksize=150000, usecols=['UF_ZI', 'VAL_TOT', 'DIAS_PERM', 'MORTE', 'IDADE', 'SEXO', 'CAR_INT', 'COMPLEX']):
        # Amostrar 5% de cada bloco para manter performance e alta representatividade (~50.000 por mês => ~600.000 total)
        sample = chunk.sample(frac=0.04, random_state=42)
        chunk_list.append(sample)
    df_month = pd.concat(chunk_list, ignore_index=True)
    sampled_chunks.append(df_month)

df_raw = pd.concat(sampled_chunks, ignore_index=True)
print(f"Base Amostral Total Carregada: {len(df_raw):,} registros de internação hospitalar (AIH).")

# 3. Tratamento de Dados e Limpeza
df = df_raw.copy()
df['UF_CODE'] = pd.to_numeric(df['UF_ZI'].astype(str).str[:2], errors='coerce')
df['Regiao'] = df['UF_CODE'].map(uf_to_region)
df['UF_Sigla'] = df['UF_CODE'].map(uf_to_name)

# Converter colunas numéricas
df['VAL_TOT'] = pd.to_numeric(df['VAL_TOT'], errors='coerce')
df['DIAS_PERM'] = pd.to_numeric(df['DIAS_PERM'], errors='coerce')
df['MORTE'] = pd.to_numeric(df['MORTE'], errors='coerce')
df['IDADE'] = pd.to_numeric(df['IDADE'], errors='coerce')
df['COMPLEX'] = pd.to_numeric(df['COMPLEX'], errors='coerce')
df['CAR_INT'] = pd.to_numeric(df['CAR_INT'], errors='coerce')

# Filtrar nulos essenciais e valores anômalos
df = df.dropna(subset=['Regiao', 'VAL_TOT', 'DIAS_PERM', 'MORTE', 'IDADE'])
df = df[(df['VAL_TOT'] > 0) & (df['DIAS_PERM'] >= 0) & (df['IDADE'] >= 0) & (df['IDADE'] <= 115)]

# Tratar outliers extremos (percentil 99.5) para evitar distorções graves em médias
q99_val = df['VAL_TOT'].quantile(0.995)
q99_dias = df['DIAS_PERM'].quantile(0.995)

df_clean = df[(df['VAL_TOT'] <= q99_val) & (df['DIAS_PERM'] <= q99_dias)].copy()
df_clean['LOG_VAL_TOT'] = np.log1p(df_clean['VAL_TOT'])

print(f"Base Pós-Limpeza e Remoção de Outliers: {len(df_clean):,} registros.")

# 4. Análise Estatística Descritiva por Região
desc_summary = df_clean.groupby('Regiao').agg(
    N_Internacoes=('VAL_TOT', 'count'),
    Custo_Medio=('VAL_TOT', 'mean'),
    Custo_Mediana=('VAL_TOT', 'median'),
    Custo_DP=('VAL_TOT', 'std'),
    Permanencia_Media=('DIAS_PERM', 'mean'),
    Permanencia_Mediana=('DIAS_PERM', 'median'),
    Taxa_Mortalidade_Pct=('MORTE', lambda x: x.mean() * 100),
    Idade_Media=('IDADE', 'mean')
).reset_index()

# Ordenar regiões por volume
order_regions = ['Norte', 'Nordeste', 'Sudeste', 'Sul', 'Centro-Oeste']
desc_summary['Regiao'] = pd.Categorical(desc_summary['Regiao'], categories=order_regions, ordered=True)
desc_summary = desc_summary.sort_values('Regiao').reset_index(drop=True)

print("\n=== ESTATÍSTICA DESCRITIVA POR MACRORREGIÃO ===")
print(desc_summary.to_string(index=False))

# Save summary to JSON for reporting
desc_summary.to_json('scratch/desc_summary.json', orient='records', indent=2)

# 5. Testes de Hipótese (ANOVA & Kruskal-Wallis)
print("\n=== TESTES ESTATÍSTICOS DE HIPÓTESE ===")

# A) ANOVA de Um Fator para Custo Hospitalar (LOG_VAL_TOT)
groups_custo = [group['LOG_VAL_TOT'].values for name, group in df_clean.groupby('Regiao')]
f_val_custo, p_val_custo = stats.f_oneway(*groups_custo)
print(f"ANOVA (Custo logaritmitzado): F = {f_val_custo:.4f}, p-valor = {p_val_custo:.4e}")

# Kruskal-Wallis (Não paramétrico)
h_val_custo, p_kruskal_custo = stats.kruskal(*groups_custo)
print(f"Kruskal-Wallis (Custo): H = {h_val_custo:.4f}, p-valor = {p_kruskal_custo:.4e}")

# B) ANOVA & Kruskal-Wallis para Tempo de Permanência (DIAS_PERM)
groups_dias = [group['DIAS_PERM'].values for name, group in df_clean.groupby('Regiao')]
f_val_dias, p_val_dias = stats.f_oneway(*groups_dias)
h_val_dias, p_kruskal_dias = stats.kruskal(*groups_dias)
print(f"ANOVA (Dias Permanência): F = {f_val_dias:.4f}, p-valor = {p_val_dias:.4e}")
print(f"Kruskal-Wallis (Dias Permanência): H = {h_val_dias:.4f}, p-valor = {p_kruskal_dias:.4e}")

# C) Teste Qui-Quadrado para Taxa de Mortalidade
ct = pd.crosstab(df_clean['Regiao'], df_clean['MORTE'])
chi2, p_chi2, dof, ex = stats.chi2_contingency(ct)
print(f"Qui-Quadrado (Mortalidade vs Região): Chi2 = {chi2:.4f}, dof = {dof}, p-valor = {p_chi2:.4e}")

# D) Teste Post-Hoc de Tukey para Custo Hospitalar
tukey = pairwise_tukeyhsd(endog=df_clean['LOG_VAL_TOT'], groups=df_clean['Regiao'], alpha=0.05)
print("\n=== TESTE POST-HOC DE TUKEY (CUSTO LOGARÍTMICO) ===")
print(tukey)

# E) Modelo de Regressão Linear Múltipla (OLS)
ols_model = smf.ols('LOG_VAL_TOT ~ C(Regiao, Treatment(reference="Sudeste")) + DIAS_PERM + IDADE + C(CAR_INT)', data=df_clean).fit()
print("\n=== MODELO DE REGRESSÃO LINEAR MÚLTIPLA (OLS) ===")
print(ols_model.summary())

# Guardar resultados dos testes em arquivo
test_results = {
    'f_val_custo': float(f_val_custo),
    'p_val_custo': float(p_val_custo),
    'h_val_custo': float(h_val_custo),
    'p_kruskal_custo': float(p_kruskal_custo),
    'f_val_dias': float(f_val_dias),
    'p_val_dias': float(p_val_dias),
    'h_val_dias': float(h_val_dias),
    'p_kruskal_dias': float(p_kruskal_dias),
    'chi2_morte': float(chi2),
    'p_chi2_morte': float(p_chi2),
    'r2_ols': float(ols_model.rsquared),
    'r2_adj_ols': float(ols_model.rsquared_adj),
    'f_ols': float(ols_model.fvalue),
    'p_ols': float(ols_model.f_pvalue)
}
with open('scratch/test_results.json', 'w') as f_out:
    json.dump(test_results, f_out, indent=2)

# 6. GERAÇÃO DE GRÁFICOS EM ALTA RESOLUÇÃO
os.makedirs('artigo/figuras', exist_ok=True)

# Figura 1: Boxplot do Custo por Internação por Região
fig, ax = plt.subplots(figsize=(8, 4.2), dpi=300)
palette = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
sns.boxplot(data=df_clean, x='Regiao', y='VAL_TOT', order=order_regions, palette=palette, ax=ax, fliersize=1, boxprops=dict(alpha=0.8))
ax.set_yscale('log')
ax.set_xlabel('Macrorregião Brasileira', fontsize=10, fontweight='bold')
ax.set_ylabel('Custo Total da Internação (R$ - Escala Log)', fontsize=10, fontweight='bold')
ax.grid(True, linestyle='--', alpha=0.5, which='both')
plt.tight_layout()
plt.savefig('artigo/figuras/fig1_custo_regiao.png')
plt.close()

# Figura 2: Tempo Médio de Permanência e Taxa de Mortalidade
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.2), dpi=300)

# 2A: Permanência Média
bars1 = ax1.bar(desc_summary['Regiao'], desc_summary['Permanencia_Media'], color='#2b5c8f', edgecolor='black', linewidth=0.7)
ax1.set_title('(A) Tempo Médio de Permanência (Dias)', fontsize=10, fontweight='bold')
ax1.set_ylabel('Média de Dias por Internação', fontsize=9)
ax1.set_ylim(0, desc_summary['Permanencia_Media'].max() * 1.25)
for bar in bars1:
    yval = bar.get_height()
    ax1.text(bar.get_x() + bar.get_width()/2.0, yval + 0.15, f"{yval:.2f}d", ha='center', va='bottom', fontsize=8, fontweight='bold')

# 2B: Taxa de Mortalidade
bars2 = ax2.bar(desc_summary['Regiao'], desc_summary['Taxa_Mortalidade_Pct'], color='#c0392b', edgecolor='black', linewidth=0.7)
ax2.set_title('(B) Taxa de Mortalidade Hospitalar (%)', fontsize=10, fontweight='bold')
ax2.set_ylabel('Óbitos por 100 Internações (%)', fontsize=9)
ax2.set_ylim(0, desc_summary['Taxa_Mortalidade_Pct'].max() * 1.25)
for bar in bars2:
    yval = bar.get_height()
    ax2.text(bar.get_x() + bar.get_width()/2.0, yval + 0.12, f"{yval:.2f}%", ha='center', va='bottom', fontsize=8, fontweight='bold')

plt.tight_layout()
plt.savefig('artigo/figuras/fig2_permanencia_mortalidade.png')
plt.close()

# Figura 3: Heatmap de Correlação
fig, ax = plt.subplots(figsize=(6, 4.2), dpi=300)
corr_vars = df_clean[['VAL_TOT', 'DIAS_PERM', 'MORTE', 'IDADE']].corr()
corr_vars.columns = ['Custo Total', 'Dias Permanência', 'Óbito (Morte)', 'Idade']
corr_vars.index = ['Custo Total', 'Dias Permanência', 'Óbito (Morte)', 'Idade']
sns.heatmap(corr_vars, annot=True, fmt='.3f', cmap='Blues', vmin=-0.1, vmax=1.0, cbar=True, ax=ax, linewidths=0.5)
plt.tight_layout()
plt.savefig('artigo/figuras/fig3_heatmap_correlacao.png')
plt.close()

# Figura 4: Coeficientes do Modelo OLS
params = ols_model.params.drop('Intercept')
conf = ols_model.conf_int().drop('Intercept')
conf['coef'] = params

fig, ax = plt.subplots(figsize=(8, 4.5), dpi=300)
y_pos = np.arange(len(params))
ax.errorbar(conf['coef'], y_pos, xerr=[conf['coef'] - conf[0], conf[1] - conf['coef']], fmt='o', color='#1b4f72', ecolor='#2980b9', elinewidth=2, capsize=4)
ax.axvline(x=0, color='red', linestyle='--', linewidth=1)
ax.set_yticks(y_pos)
clean_labels = [str(x) for x in params.index]
ax.set_yticklabels(clean_labels, fontsize=8)
ax.set_xlabel('Coeficiente $\\beta$ de Regressão (Impacto no Log-Custo)', fontsize=10, fontweight='bold')
ax.grid(True, linestyle='--', alpha=0.5)
plt.tight_layout()
plt.savefig('artigo/figuras/fig4_coeficientes_regressao.png')
plt.close()

print("\n=== GRÁFICOS GERADOS SEM TÍTULOS EMBUTIDOS COM SUCESSO EM artigo/figuras/ ===")
