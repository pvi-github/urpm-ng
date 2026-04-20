
  Dette technique identifiée (supplements/weak deps)

  ┌─────┬─────────┬──────────────────────────────────────────────────────────────────────────────┬────────┐
  │  #  │  Prio   │                                    Sujet                                     │ Effort │
  ├─────┼─────────┼──────────────────────────────────────────────────────────────────────────────┼────────┤
  │ 3   │ MOYENNE │ --with-suggests ignoré en mode upgrade                                       │ Petit  │
  ├─────┼─────────┼──────────────────────────────────────────────────────────────────────────────┼────────┤
  │ 5   │ BASSE   │ _supplement_repo_requires ne couvre que REQUIRES                             │ Petit  │
  ├─────┼─────────┼──────────────────────────────────────────────────────────────────────────────┼────────┤
  │ 6   │ BASSE   │ find_all_orphans n'inclut pas SUGGESTNAME                                    │ Petit  │
  └─────┴─────────┴──────────────────────────────────────────────────────────────────────────────┴────────┘                      
   
  Infra stabilité / perf

  ┌─────┬─────────┬────────────────────────────────────────────────────────────────────────────────────────────────┬─────────┐
  │  #  │  Prio   │                                             Sujet                                              │ Effort  │
  ├─────┼─────────┼────────────────────────────────────────────────────────────────────────────────────────────────┼─────────┤
  │ B   │ MOYENNE │ Fix libsolv add_mdk (PREREQMARKER + 35 droppés) → élimine la rustine -1.9s/pool                │ Gros    │
  │     │         │                                                                                                │ (C)     │
  ├─────┼─────────┼────────────────────────────────────────────────────────────────────────────────────────────────┼─────────┤
  │ C   │ BASSE   │ Bugs annexes libsolv (@recommends@ offset, @supplements@/@enhances@ non parsés)                │ Petit   │
  │     │         │                                                                                                │ (C)     │
  └─────┴─────────┴────────────────────────────────────────────────────────────────────────────────────────────────┴─────────┘

  Fait

  - [A] Media Update Lock — voir doc/archives/DONE_MU_LOCK.md
  - [G] Fichier de préférences /etc/urpm/conf.d/ pour --prefer persistants (settings.py:36, 245-254)
  - [H] Feedback utilisateur : contrôle auto-ajout serveurs + filtrage géo (server_pool.py:68-115)
  - [T1] test_resolver.py (couverture résolveur en place)
  - [#1] Orphan detector honore désormais Supplements/Enhances — cf3264d
  - [#2] add_local_rpms charge supplements/enhances — cf3264d
  - [#4] Setting install_recommends opérationnel (lu par resolver.py:364,390,715,1275 ; settings.py:292-293 charge depuis conf)
                                                                                                                                 
  Fonctionnalités P2P (schéma en place, code manquant)                                                                           
   
  ┌─────┬─────────┬────────────────────────────────────────────────────────┬────────┐                                            
  │  #  │  Prio   │                         Sujet                          │ Effort │
  ├─────┼─────────┼────────────────────────────────────────────────────────┼────────┤
  │ D   │ MOYENNE │ replication_policy (none/on_demand/seed) — enforcement │ Moyen  │
  ├─────┼─────────┼────────────────────────────────────────────────────────┼────────┤
  │ E   │ MOYENNE │ quota_mb — vérification et éviction                    │ Moyen  │                                            
  ├─────┼─────────┼────────────────────────────────────────────────────────┼────────┤                                            
  │ F   │ MOYENNE │ retention_days — nettoyage temporel scheduler          │ Petit  │                                            
  └─────┴─────────┴────────────────────────────────────────────────────────┴────────┘                                            
                  
  Roadmap Phase 1 (doc/ROADMAP.md)                                                                                               
                  
  ┌─────┬─────────┬────────────────────────────────────────────────────┬────────┐                                                
  │  #  │  Prio   │                       Sujet                        │ Effort │
  ├─────┼─────────┼────────────────────────────────────────────────────┼────────┤                                                
  │ P1a │ HAUTE   │ urpm system-upgrade (dist upgrade N→N+1)           │ Gros   │
  ├─────┼─────────┼────────────────────────────────────────────────────┼────────┤
  │ P1b │ HAUTE   │ Groups/rpmsrate (sélection par catégorie)          │ Moyen  │                                                
  ├─────┼─────────┼────────────────────────────────────────────────────┼────────┤                                                
  │ P1c │ MOYENNE │ needs-restarting (détection services à redémarrer) │ Moyen  │                                                
  ├─────┼─────────┼────────────────────────────────────────────────────┼────────┤                                                
  │ P1d │ MOYENNE │ builddep (installer deps de build d'un SRPM)       │ Petit  │
  ├─────┼─────────┼────────────────────────────────────────────────────┼────────┤                                                
  │ P1e │ BASSE   │ dnf-automatic (updates programmées)                │ Moyen  │
  └─────┴─────────┴────────────────────────────────────────────────────┴────────┘                                                
                  
  Tests / CI                                                                                                                     
                  
  ┌─────┬─────────┬───────────────────────────────────────────────────┬────────┐
  │  #  │  Prio   │                       Sujet                       │ Effort │
  ├─────┼─────────┼───────────────────────────────────────────────────┼────────┤
  │ T2  │ MOYENNE │ GitHub Actions CI                                 │ Petit  │                                                 
  └─────┴─────────┴───────────────────────────────────────────────────┴────────┘      


