import { createApp } from 'vue'
const workbench = new URLSearchParams(window.location.search).get('view') === 'interests'
const entry = workbench ? import('./interest/InterestDesk.vue') : import('./App.vue')
void entry.then(({ default: component }) => createApp(component).mount('#app'))
