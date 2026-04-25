const demoData = {
  builder: {
    title: 'Indie builder on a deep-work day',
    summary: 'Your strongest work happens when you stay in one build loop. The biggest drag is research spillover after lunch.',
    metrics: [
      ['Focus score', '74'],
      ['App switches/hr', '18'],
      ['Deep work', '2.4h'],
      ['Top distraction', 'YouTube'],
    ],
    findings: [
      'Rapid switching spikes after lunch: 29 switches/hr between 1 PM and 3 PM.',
      'Chrome to code bouncing is the biggest pattern reducing sustained work time.',
      'YouTube appears in short bursts that usually lead to 20–30 minutes of drift.',
    ],
    plan: 'Tomorrow, block video sites from 1 PM to 4 PM, keep one task list open, and cap research tabs at 5 before returning to code.',
    chat: {
      biggest: {
        user: 'What is the biggest problem here?',
        ai: 'The biggest issue is repeated context switching. The demo day shows 18 app switches per hour, with the worst spikes happening after lunch when browser research turns into drift.'
      },
      tomorrow: {
        user: 'What should I do tomorrow?',
        ai: 'Protect your first 90 minutes for building only, then use a timed research block later. Keep YouTube blocked during your main execution window.'
      },
      rule: {
        user: 'Give me one simple rule.',
        ai: 'If you open a sixth research tab, stop researching and write down the next action before opening anything else.'
      }
    }
  },
  student: {
    title: 'Student balancing lectures and assignment work',
    summary: 'Your main issue is mode mixing: review, study, and distraction all happen in the same session, which weakens deep work.',
    metrics: [
      ['Focus score', '61'],
      ['App switches/hr', '24'],
      ['Deep work', '1.3h'],
      ['Top distraction', 'Reddit'],
    ],
    findings: [
      'Lecture notes and social tabs are mixed in the same session, raising switching rate.',
      'Deep work blocks are too short to finish problem sets cleanly.',
      'Evening fatigue creates the sharpest drop in focus score.',
    ],
    plan: 'Split study into two modes: capture notes first, solve problems second. Keep social apps blocked until the assignment block is complete.',
    chat: {
      biggest: {
        user: 'What hurts focus most?',
        ai: 'The main drag is mixing passive lecture review with active assignment work. That forces too many context shifts and prevents longer solving blocks.'
      },
      tomorrow: {
        user: 'How should tomorrow look?',
        ai: 'Start with a 45-minute review block, then switch into a 60-minute assignment block with all social tabs closed. Treat them as separate sessions.'
      },
      rule: {
        user: 'What rule should I follow?',
        ai: 'No Reddit until the first assignment block is submitted or fully drafted.'
      }
    }
  },
  designer: {
    title: 'Designer switching between tools and references',
    summary: 'Your design workflow is solid once you stay inside production mode. The expensive part is reference drift in the browser.',
    metrics: [
      ['Focus score', '68'],
      ['App switches/hr', '21'],
      ['Deep work', '1.9h'],
      ['Top distraction', 'Instagram'],
    ],
    findings: [
      'Reference gathering turns into style browsing too often.',
      'Tool changes are normal, but browser changes are inflating perceived work complexity.',
      'The strongest improvement opportunity is reducing visual-bait loops during concept work.',
    ],
    plan: 'Create a reference collection window first, then close the browser and move into production mode. Use one review checkpoint instead of constant browsing.',
    chat: {
      biggest: {
        user: 'What is the main issue?',
        ai: 'The main issue is not design-tool switching. It is browser-based reference drift, especially when inspiration browsing extends into social-style scrolling.'
      },
      tomorrow: {
        user: 'What should I change tomorrow?',
        ai: 'Collect references for 20 minutes max, save them, then close the browser entirely while doing the first draft pass.'
      },
      rule: {
        user: 'One rule please.',
        ai: 'Once the reference board is ready, no more browsing until the first design version exists.'
      }
    }
  }
};

let activePersona = 'builder';

function setPersona(name) {
  activePersona = name;
  const data = demoData[name];
  document.querySelectorAll('.persona').forEach(btn => {
    btn.classList.toggle('is-active', btn.dataset.persona === name);
  });
  document.getElementById('demoTitle').textContent = data.title;
  document.getElementById('demoMetrics').innerHTML = data.metrics
    .map(([label, value]) => `<div class="demo-metric"><span>${label}</span><strong>${value}</strong></div>`)
    .join('');
  const summary = document.getElementById('demoSummary');
  if (summary) summary.textContent = data.summary;
  document.getElementById('demoFindings').innerHTML = data.findings
    .map(item => `<li>${item}</li>`)
    .join('');
  document.getElementById('demoPlan').textContent = data.plan;
  const stats = {
    statFocus: data.metrics[0][1],
    statSwitches: data.metrics[1][1],
    statDeep: data.metrics[2][1],
    statDistraction: data.metrics[3][1],
  };
  Object.entries(stats).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  });
  renderDemoChat('biggest');
}

function renderDemoChat(key) {
  const data = demoData[activePersona].chat[key];
  const chat = document.getElementById('demoChat');
  chat.innerHTML = `
    <div class="demo-msg user">${data.user}</div>
    <div class="demo-msg ai">${data.ai}</div>
  `;
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.persona').forEach(btn => {
    btn.addEventListener('click', () => setPersona(btn.dataset.persona));
  });
  document.querySelectorAll('.mini-btn').forEach(btn => {
    btn.addEventListener('click', () => renderDemoChat(btn.dataset.prompt));
  });
  document.querySelectorAll('.fake-send').forEach(btn => {
    btn.addEventListener('click', () => renderDemoChat('tomorrow'));
  });
  setPersona(activePersona);
});
